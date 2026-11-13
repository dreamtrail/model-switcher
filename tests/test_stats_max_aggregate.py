from __future__ import annotations

import json
import math

import pytest

from src.stats import STATS_HTML, StatsCollector


# ---------------------------------------------------------------------------
# Unit: maximum aggregate generation throughput (P-20260918-05, AC1-AC8)
# ---------------------------------------------------------------------------


class TestMaxAggregateGenerationThroughput:
    """Collector-contract tests with plan-defined interval timestamps."""

    @staticmethod
    def _start(collector, model, request_id, started_at):
        collector.record_decode_start(model, request_id, started_at)

    @staticmethod
    def _end(
        collector, model, request_id, ended_at, completion_tokens, *, ok=True,
    ):
        collector.record_decode_end(
            model,
            request_id,
            ended_at=ended_at,
            ok=ok,
            completion_tokens=completion_tokens,
        )

    @staticmethod
    def _maximum(collector, model):
        return collector.snapshot()["models"][model]["requests"]["rates"][
            "generation_tps"
        ]["max_aggregate"]

    def test_overlapping_cohort_uses_exact_tokens_span_and_peak_concurrency(
        self,
    ):
        # AC1: [0,10), [2,8), and [4,6) peak at three simultaneous decodes.
        # Exact cohort formula: (10 + 20 + 30) / (10 - 0) = 6 t/s.
        collector = StatsCollector()
        for request_id, started_at in (("a", 0.0), ("b", 2.0), ("c", 4.0)):
            self._start(collector, "m", request_id, started_at)

        self._end(collector, "m", "c", 6.0, 30)
        self._end(collector, "m", "b", 8.0, 20)
        assert self._maximum(collector, "m") is None  # cohort still active
        self._end(collector, "m", "a", 10.0, 10)

        maximum = self._maximum(collector, "m")
        assert maximum["tps"] == pytest.approx((10 + 20 + 30) / (10.0 - 0.0))
        assert maximum["concurrency"] == 3

    def test_transitive_positive_overlap_forms_one_maximal_cohort(self):
        # AC2: A overlaps B and B overlaps C, although A does not overlap C.
        collector = StatsCollector()
        for request_id, started_at in (("a", 0.0), ("b", 2.0), ("c", 4.0)):
            self._start(collector, "m", request_id, started_at)

        self._end(collector, "m", "a", 3.0, 7)
        self._end(collector, "m", "b", 5.0, 14)
        self._end(collector, "m", "c", 7.0, 21)

        assert self._maximum(collector, "m") == {
            "tps": pytest.approx((7 + 14 + 21) / (7.0 - 0.0)),
            "concurrency": 2,
        }

    def test_touching_half_open_intervals_are_independent_cohorts(self):
        # AC1/AC2: [0,2) and [2,4) have no positive overlap. The first
        # cohort can publish while the second remains active, each at @1.
        collector = StatsCollector()
        self._start(collector, "m", "first", 0.0)
        self._start(collector, "m", "second", 2.0)

        self._end(collector, "m", "first", 2.0, 10)
        assert self._maximum(collector, "m") == {"tps": 5.0, "concurrency": 1}

        self._end(collector, "m", "second", 4.0, 12)
        assert self._maximum(collector, "m") == {"tps": 6.0, "concurrency": 1}

    def test_ineligible_bridge_removal_publishes_earlier_component_incrementally(
        self,
    ):
        # AC2: the unresolved bridge provisionally overlaps the early cohort.
        # Once it ends ineligible it cannot connect that cohort to later work.
        collector = StatsCollector()
        self._start(collector, "m", "early", 0.0)
        self._start(collector, "m", "bridge", 3.0)
        self._end(collector, "m", "early", 4.0, 12)
        assert self._maximum(collector, "m") is None

        self._start(collector, "m", "later", 10.0)
        self._end(collector, "m", "bridge", 12.0, 100, ok=False)

        # The unrelated later decode is still active, but the early cohort is
        # now final and publishes immediately: 12 / (4 - 0) = 3 t/s.
        assert self._maximum(collector, "m") == {"tps": 3.0, "concurrency": 1}
        self._end(collector, "m", "later", 14.0, 20)
        assert self._maximum(collector, "m") == {"tps": 5.0, "concurrency": 1}

    def test_maximum_replaces_only_on_strict_increase_and_keeps_first_tie(self):
        # AC5: the equal-rate cohort has higher concurrency, making a tie
        # replacement observable; the first 5@1 result must remain.
        collector = StatsCollector()
        self._start(collector, "m", "first", 0.0)
        self._end(collector, "m", "first", 2.0, 10)

        self._start(collector, "m", "lower", 5.0)
        self._end(collector, "m", "lower", 7.0, 6)
        assert self._maximum(collector, "m") == {"tps": 5.0, "concurrency": 1}

        self._start(collector, "m", "tie-a", 10.0)
        self._start(collector, "m", "tie-b", 11.0)
        self._end(collector, "m", "tie-b", 13.0, 10)
        self._end(collector, "m", "tie-a", 14.0, 10)
        assert self._maximum(collector, "m") == {"tps": 5.0, "concurrency": 1}

        self._start(collector, "m", "higher-a", 20.0)
        self._start(collector, "m", "higher-b", 21.0)
        self._end(collector, "m", "higher-b", 23.0, 12)
        self._end(collector, "m", "higher-a", 24.0, 12)
        assert self._maximum(collector, "m") == {"tps": 6.0, "concurrency": 2}

    def test_huge_exact_tokens_do_not_poison_maximum_or_json(self):
        # AC1/AC5/AC6 regression: converting an exact integer this large to a
        # float would overflow. Recording must remain non-raising, publish no
        # non-finite value, and leave the maximum open to a later valid cohort.
        collector = StatsCollector()
        self._start(collector, "m", "huge", 0.0)
        self._end(collector, "m", "huge", 1.0, 10**400)

        assert self._maximum(collector, "m") is None
        model_stats = collector._models["m"]
        assert model_stats.max_aggregate_tps is None
        assert model_stats.max_aggregate_concurrency is None
        json.dumps(collector.snapshot(), allow_nan=False)

        self._start(collector, "m", "finite", 10.0)
        self._end(collector, "m", "finite", 12.0, 12)

        assert self._maximum(collector, "m") == {
            "tps": 6.0,
            "concurrency": 1,
        }
        json.dumps(collector.snapshot(), allow_nan=False)

    def test_higher_unrounded_rate_replaces_equal_display_rate(self):
        # AC5: both candidates display as 107.7 to one decimal, but maximum
        # comparison uses their distinct unrounded rates. Distinct concurrency
        # makes replacement by the 107.74 candidate directly observable.
        collector = StatsCollector()
        self._start(collector, "m", "first", 0.0)
        self._end(collector, "m", "first", 100.0, 10_771)
        first = self._maximum(collector, "m")

        self._start(collector, "m", "higher-a", 200.0)
        self._start(collector, "m", "higher-b", 225.0)
        self._end(collector, "m", "higher-b", 275.0, 5_774)
        self._end(collector, "m", "higher-a", 300.0, 5_000)
        higher = self._maximum(collector, "m")

        assert first == {"tps": pytest.approx(107.71), "concurrency": 1}
        assert higher == {"tps": pytest.approx(107.74), "concurrency": 2}
        assert f"{first['tps']:.1f}" == f"{higher['tps']:.1f}" == "107.7"

    def test_maximum_is_per_model_and_resets_with_a_fresh_collector(self):
        # AC5: lifetime scope is one collector/process, isolated by model.
        collector = StatsCollector()
        self._start(collector, "a", "a-request", 0.0)
        self._end(collector, "a", "a-request", 2.0, 10)
        self._start(collector, "b", "b-request", 0.0)
        self._end(collector, "b", "b-request", 3.0, 21)

        assert self._maximum(collector, "a") == {"tps": 5.0, "concurrency": 1}
        assert self._maximum(collector, "b") == {"tps": 7.0, "concurrency": 1}

        fresh = StatsCollector()
        fresh.record_model_start("a")
        assert self._maximum(fresh, "a") is None

    def test_duplicate_start_and_end_are_idempotent_by_request_identity(self):
        # AC3: the first observation owns the identity; repeated completion is
        # harmless and cannot replace its exact final usage.
        collector = StatsCollector()
        self._start(collector, "m", "request", 0.0)
        self._start(collector, "m", "request", 99.0)
        self._end(collector, "m", "request", 2.0, 10)
        self._end(collector, "m", "request", 3.0, 999)

        assert self._maximum(collector, "m") == {"tps": 5.0, "concurrency": 1}

    def test_all_ineligible_decode_outcomes_leave_maximum_null(self):
        # AC4: each model exercises one exclusion without sharing state.
        collector = StatsCollector()
        cases = {
            "missing-usage": (0.0, 1.0, None, True),
            "zero-tokens": (0.0, 1.0, 0, True),
            "zero-duration": (1.0, 1.0, 5, True),
            "negative-duration": (2.0, 1.0, 5, True),
            "nonfinite-timing": (0.0, math.inf, 5, True),
            "failure": (0.0, 1.0, 5, False),
            "stream-error": (0.0, 1.0, 5, False),
            "cancellation": (0.0, 1.0, 5, False),
        }
        for model, (started_at, ended_at, tokens, ok) in cases.items():
            self._start(collector, model, model, started_at)
            self._end(collector, model, model, ended_at, tokens, ok=ok)

        # Missing first-token/timing: an end for an unknown identity is a no-op.
        collector.record_model_start("missing-first-token")
        self._end(collector, "missing-first-token", "never-started", 1.0, 5)
        # Non-streaming usage still belongs to existing token metrics only.
        collector.record_request(
            "non-stream", ok=True, completion_tokens=50,
        )

        for model in (*cases, "missing-first-token", "non-stream"):
            assert self._maximum(collector, model) is None

    def test_nonpositive_synthetic_duration_keeps_existing_average_behavior(self):
        # AC4/AC8 compatibility: the new positive-duration rule must not be
        # backported into the established weighted-average accumulator.
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, completion_tokens=10, decode_s=1.0,
        )
        collector.record_request(
            "m", ok=True, completion_tokens=5, decode_s=-0.25,
        )

        generation = collector.snapshot()["models"]["m"]["requests"][
            "rates"
        ]["generation_tps"]
        assert generation["avg"] == pytest.approx((10 + 5) / (1.0 - 0.25))
        assert generation["max_aggregate"] is None

    def test_global_and_per_model_generation_rate_shapes_are_deliberately_distinct(
        self,
    ):
        # AC6: max_aggregate is per-model only and null before completion.
        collector = StatsCollector()
        collector.record_model_start("m")
        snap = collector.snapshot()

        # avg_aggregate (P-20260925-04) is present in both scopes.
        assert snap["requests"]["rates"]["generation_tps"] == {
            "avg": None,
            "last_60s": 0.0,
            "last_300s": 0.0,
            "avg_aggregate": None,
            "server_timed_share": None,
            "skipped_buffered": 0,
            "live": {
                "tps": 0.0, "window_s": 5, "estimate": True,
                "source": "estimate", "busy_s": 0.0, "tokens_per_chunk": None,
            },
        }
        assert snap["models"]["m"]["requests"]["rates"]["generation_tps"] == {
            "avg": None,
            "last_60s": 0.0,
            "last_300s": 0.0,
            "max_aggregate": None,
            "avg_aggregate": None,
            "server_timed_share": None,
            "skipped_buffered": 0,
            "live": {
                "tps": 0.0, "window_s": 5, "estimate": True,
                "source": "estimate", "busy_s": 0.0, "tokens_per_chunk": 1.0,
            },
        }

    def test_dashboard_drops_best_column_but_json_keeps_maximum(self):
        # P-20260925-04 rev 2: the "Best gen t/s" column is removed from the
        # page; max_aggregate stays in the per-model JSON (additive contract).
        assert "Best gen t/s" not in STATS_HTML
        assert "max_aggregate" not in STATS_HTML
        collector = StatsCollector()
        collector.record_model_start("m")
        generation = collector.snapshot()["models"]["m"]["requests"][
            "rates"
        ]["generation_tps"]
        assert "max_aggregate" in generation

    def test_dashboard_keeps_safe_dom_writes_and_refresh_behavior(self):
        # AC7/AC8: text-only rendering and existing polling are unchanged.
        assert ".innerHTML" not in STATS_HTML
        assert ".textContent" in STATS_HTML
        assert 'fetch("/stats/json")' in STATS_HTML
        assert "setInterval(refresh, 5000)" in STATS_HTML


