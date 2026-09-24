from __future__ import annotations

import json
import math
import shutil
import subprocess

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

        assert snap["requests"]["rates"]["generation_tps"] == {
            "avg": None,
            "last_60s": 0.0,
            "last_300s": 0.0,
        }
        assert snap["models"]["m"]["requests"]["rates"]["generation_tps"] == {
            "avg": None,
            "last_60s": 0.0,
            "last_300s": 0.0,
            "max_aggregate": None,
        }

    def test_dashboard_has_best_generation_header_and_field_wiring(self):
        # AC7, updated for the compact table (P-20260925-01): the maximum is
        # the "Best gen t/s" column and is wired to the per-model field only.
        assert "<th>Best gen t/s</th>" in STATS_HTML
        assert "withBest ? fmtMaxAggregateTps(gt.max_aggregate) : DASH" \
            in STATS_HTML

    @staticmethod
    def _execute_dashboard_javascript(expression):
        """Run the actual embedded script with a minimal, browserless DOM."""
        # Resolve Node through PATH rather than assuming a platform-specific
        # install location. It is a required test prerequisite because AC7
        # must execute the embedded JavaScript rather than silently skip it.
        node = shutil.which("node")
        if node is None:
            pytest.fail(
                "Node.js is required to execute the embedded-JS AC7 tests"
            )
        script = STATS_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r'''
class Element {
  constructor(tagName, id = null) {
    this.tagName = tagName;
    this.id = id;
    this.children = [];
    this._textContent = "";
    this.className = "";
    this.hidden = false;
    this.style = {};
  }
  set textContent(value) {
    this._textContent = String(value);
    if (value === "") this.children = [];
  }
  setAttribute() {}
  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent).join("")
      : this._textContent;
  }
  appendChild(child) { this.children.push(child); return child; }
}
const elements = new Map(
  ["models", "totals", "errors"].map((id) => [id, new Element("div", id)])
);
const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement(tagName) { return new Element(tagName); },
};
function fetch() { return new Promise(() => {}); }
function setInterval() { return 0; }
'''
        completed = subprocess.run(
            [node, "-"],
            input=harness + script + "\n" + expression,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    @pytest.mark.parametrize(
        ("value_expression", "expected"),
        [
            ("{tps: 107.76, concurrency: 3}", "107.8 @3"),
            ("null", "–"),
            ("42", "–"),
            ('"primitive"', "–"),
            ("true", "–"),
            ("[]", "–"),
            ("{tps: NaN, concurrency: 1}", "–"),
            ("{tps: Infinity, concurrency: 1}", "–"),
            ("{tps: -Infinity, concurrency: 1}", "–"),
            ("{tps: 0, concurrency: 1}", "–"),
            ("{tps: -1, concurrency: 1}", "–"),
            ("{concurrency: 1}", "–"),
            ("{tps: 1}", "–"),
            ("{tps: 1, concurrency: 1.5}", "–"),
            ("{tps: 1, concurrency: 0}", "–"),
            ("{tps: 1, concurrency: -1}", "–"),
        ],
    )
    def test_dashboard_executes_maximum_formatter_contract(
        self, value_expression, expected,
    ):
        # AC7: execute the shipped formatter itself, including JavaScript-only
        # non-finite values that cannot be represented in strict JSON.
        output = self._execute_dashboard_javascript(
            "console.log(JSON.stringify(fmtMaxAggregateTps("
            + value_expression
            + ")));"
        )
        assert output == expected

    def test_dashboard_rendered_maximum_cell_has_observable_text(self):
        # AC7: exercise the actual table renderer and DOM textContent result,
        # not merely the formatter's source or return value.
        output = self._execute_dashboard_javascript(r'''
render({models: {
  "model-a": {
    requests: {
      tokens: {
        prompt: 12, completion: 9, cached: 3, prefill: 9,
        cache_hit_rate: 0.25,
      },
      rates: {
        prompt_tps: {avg: 4},
        generation_tps: {
          avg: 9,
          max_aggregate: {tps: 107.76, concurrency: 3},
        },
      },
      ttft_ms: {p50: 10, p95: 20},
    },
  },
}});
const rendered = document.getElementById("models");
const totals = document.getElementById("totals");
console.log(JSON.stringify({
  rows: rendered.children.length + totals.children.length,
  cells: rendered.children[0].children.length,
  maximumText: rendered.children[0].children[4].textContent,
  allRowMaximumText: totals.children[0].children[4].textContent,
}));
''')
        # One model row plus the "All models" totals row (in <tfoot> since
        # P-20260925-01 rev 2), 8 columns; the global row never
        # shows a maximum because the global block has no max_aggregate.
        assert output == {
            "rows": 2, "cells": 8,
            "maximumText": "107.8 @3", "allRowMaximumText": "–",
        }

    def test_dashboard_formats_valid_maximum_to_one_decimal_at_concurrency(self):
        # Preserve the static wiring gate alongside executable JS coverage.
        assert 'return v.tps.toFixed(1) + " @" + v.concurrency;' in STATS_HTML

    @pytest.mark.parametrize(
        "invalid_contract",
        [
            # null, primitive/non-object, and array values
            'if (!v || typeof v !== "object" || Array.isArray(v)) return DASH;',
            # missing/non-number/nonfinite/nonpositive tps
            'typeof v.tps === "number" && Number.isFinite(v.tps) && v.tps > 0',
            # missing/non-number/non-integer/nonpositive concurrency
            'typeof v.concurrency === "number" &&\n'
            '        Number.isInteger(v.concurrency) && v.concurrency > 0',
        ],
    )
    def test_dashboard_malformed_maximum_contract_falls_back_to_dash(
        self, invalid_contract,
    ):
        # AC7 static formatter contract: each complete validation guard leads
        # to DASH, without requiring a browser/runtime dependency.
        formatter = STATS_HTML.split(
            "function fmtMaxAggregateTps(v) {", 1
        )[1].split("\n}\n\nfunction fmtTtft", 1)[0]
        assert "function fmtTtft" not in formatter
        assert invalid_contract in formatter
        assert "return DASH;" in formatter
        assert 'const DASH = "–"' in STATS_HTML

    def test_dashboard_keeps_safe_dom_writes_and_refresh_behavior(self):
        # AC7/AC8: text-only rendering and existing polling are unchanged.
        assert ".innerHTML" not in STATS_HTML
        assert ".textContent" in STATS_HTML
        assert 'fetch("/stats/json")' in STATS_HTML
        assert "setInterval(refresh, 5000)" in STATS_HTML


