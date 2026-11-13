"""Live-rate and connection-indicator tests for the ``/stats`` page."""

from __future__ import annotations

import json

import pytest

from src.stats import STATS_HTML
from tests.stats_page_helpers import (
    FULL,
    FRESH,
    _cells,
    _execute,
    _render,
    _rows,
    _text,
)


class TestStatsPageLiveRate:
    """P-20260926-01: live generation rate tile and column.

    Tooltip text updated by P-20260926-04 (busy-time, calibrated) and
    P-20261114-01 (5 s window, engine counter source shown on the tile).
    """

    TIP = (
        "Combined speed while generating, last 5 s: the engine's"
        " generated-token counter (vLLM), else an estimate from streamed"
        " chunks × tokens per chunk"
    )

    def test_tile_markup_is_labelled_as_estimate_with_tooltip(self):
        tiles = STATS_HTML.split('<section class="kpis"', 1)[1].split("</section>", 1)[0]
        assert f'<div class="card tile" title="{self.TIP}">' in tiles
        assert '<div class="label">Live gen t/s</div>' in tiles
        assert '<div id="kpi-live" class="value">–</div>' in tiles
        assert '<div id="kpi-live-sub" class="sub">estimate · 5 s</div>' in tiles
        # Seven tiles: the wide model tile keeps 2/4/8-column rows full.
        assert tiles.count('class="card tile') == 7
        assert '<div class="card tile wide">' in tiles
        style = STATS_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        assert ".kpis .tile.wide { grid-column: span 2; }" in style
        assert "repeat(8, minmax(0, 1fr))" in style
        assert "setInterval(refresh, 5000)" in STATS_HTML

    @pytest.mark.parametrize(
        ("value_expression", "expected"),
        [
            ("{tps: 12.34, window_s: 5, estimate: true}", "12.3"),
            ("{tps: 0.05}", "0.1"),
            ("{tps: 0}", "–"),
            ("{tps: -1}", "–"),
            ("{tps: null}", "–"),
            ('{tps: "5"}', "–"),
            ("{tps: NaN}", "–"),
            ("{tps: Infinity}", "–"),
            ("{}", "–"),
            ("null", "–"),
            ("undefined", "–"),
            ("[]", "–"),
            ("7", "–"),
        ],
    )
    def test_live_formatter_contract(self, value_expression, expected):
        output = _execute(
            "console.log(JSON.stringify(fmtLive(" + value_expression + ")));\n"
        )
        assert output == expected

    @pytest.mark.parametrize(
        ("value_expression", "expected"),
        [
            ('{tps: 80, window_s: 5, source: "engine"}', "engine · 5 s"),
            ('{tps: 3, window_s: 5, source: "estimate"}', "estimate · 5 s"),
            ("{tps: 3, window_s: 5}", "estimate · 5 s"),
            ('{source: "other", window_s: 7}', "estimate · 7 s"),
            ('{source: "engine", window_s: "5"}', "engine · 5 s"),
            ("null", "estimate · 5 s"),
            ("[]", "estimate · 5 s"),
        ],
    )
    def test_live_source_formatter_contract(self, value_expression, expected):
        output = _execute(
            "console.log(JSON.stringify(fmtLiveSource("
            + value_expression + ")));\n"
        )
        assert output == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("engine", "engine · 5 s"), ("estimate", "estimate · 5 s")],
    )
    def test_tile_subtitle_shows_source(self, source, expected):
        page = _render({
            "requests": {"total": 1, "rates": {"generation_tps": {"live": {
                "tps": 81.5, "window_s": 5, "source": source,
                "estimate": source == "estimate",
            }}}},
            "models": {},
        })
        assert _text(page, "kpi-live") == "81.5"
        assert _text(page, "kpi-live-sub") == expected

    def test_live_values_render_per_model_and_in_all_models_row(self):
        live = lambda tps: {"tps": tps, "window_s": 5, "estimate": True}
        page = _render({
            "current_model": "a",
            "requests": {"total": 3, "rates": {"generation_tps": {"live": live(7.5)}}},
            "models": {
                "a": {"requests": {"total": 2, "rates": {
                    "generation_tps": {"live": live(5.0)}}}},
                "b": {"requests": {"total": 1, "rates": {
                    "generation_tps": {"live": live(2.5)}}}},
                "c": {"requests": {"total": 0, "rates": {
                    "generation_tps": {"live": live(0.0)}}}},
            },
        })
        assert _text(page, "kpi-live") == "7.5"
        rows = _rows(page)
        assert [(_cells(r)[0], _cells(r)[4]) for r in rows] == [
            ("a", "5.0"), ("b", "2.5"), ("c", "–"),
        ]
        assert rows[2]["children"][4]["className"] == "muted"
        assert _cells(_rows(page, "totals")[0])[4] == "7.5"


class TestStatsPageLiveIndicator:
    def test_refresh_polls_json_and_schedules_live_ticks(self):
        output = _execute(
            "(async () => {\n"
            "  await flush();\n"
            "  console.log(JSON.stringify({fetchCalls, intervals}));\n"
            "})();\n",
            responses=[{"ok": True, "status": 200, "data": FULL}],
        )
        assert output["fetchCalls"] == ["/stats/json"]
        assert {"name": "refresh", "ms": 5000} in output["intervals"]
        assert {"name": "renderLive", "ms": 1000} in output["intervals"]

    def test_live_indicator_ticks_and_keeps_last_render_on_failure(self):
        output = _execute(
            "(async () => {\n"
            "  const live = () => ({dot: elements.get('live-dot').className,\n"
            "                       text: elements.get('live-text').textContent});\n"
            "  renderLive();\n"
            "  const connecting = live();\n"
            "  await flush();\n"
            "  const first = snapshotPage();\n"
            "  const fresh = live();\n"
            "  fakeNow += 7400;\n"
            "  renderLive();\n"
            "  const ticked = live();\n"
            "  refresh();\n"
            "  await flush();\n"
            "  const afterHttpError = snapshotPage();\n"
            "  const lostHttp = live();\n"
            "  fakeNow += 5000;\n"
            "  renderLive();\n"
            "  const stillLost = live();\n"
            "  refresh();\n"
            "  await flush();\n"
            "  const lostOffline = live();\n"
            "  const afterOffline = snapshotPage();\n"
            "  responseQueue.push({ok: true, status: 200, data: FRESH_DATA});\n"
            "  refresh();\n"
            "  await flush();\n"
            "  const recovered = live();\n"
            "  const recoveredPage = snapshotPage();\n"
            "  console.log(JSON.stringify({connecting, fresh, ticked, lostHttp,\n"
            "    stillLost, lostOffline, recovered, first, afterHttpError,\n"
            "    afterOffline, recoveredPage, fetchCalls}));\n"
            "})();\n".replace("FRESH_DATA", json.dumps(FRESH)),
            responses=[
                {"ok": True, "status": 200, "data": FULL},
                {"ok": False, "status": 500, "data": FRESH},
            ],
        )
        assert output["connecting"] == {"dot": "dot", "text": "Connecting…"}
        assert output["fresh"] == {"dot": "dot good", "text": "Live · updated 0s ago"}
        assert output["ticked"] == {"dot": "dot good", "text": "Live · updated 7s ago"}
        lost = {"dot": "dot serious", "text": "Connection lost · showing last data"}
        assert output["lostHttp"] == lost
        assert output["stillLost"] == lost
        assert output["lostOffline"] == lost
        assert output["recovered"] == {"dot": "dot good", "text": "Live · updated 0s ago"}
        assert output["fetchCalls"] == ["/stats/json"] * 4

        first = output["first"]
        assert _cells(_rows(first)[0])[0] == "qwen3-32b"
        # Failures keep the last successful render (live indicator aside).
        for snapshot in (output["afterHttpError"], output["afterOffline"]):
            for key, node in first.items():
                if key not in ("live-dot", "live-text"):
                    assert snapshot[key] == node, key
        assert _text(output["recoveredPage"], "kpi-model") == "None"

    @pytest.mark.parametrize("body", [None, [], [FULL], 3, "ok", True])
    def test_non_object_200_body_counts_as_failure(self, body):
        output = _execute(
            "(async () => {\n"
            "  const live = () => ({dot: elements.get('live-dot').className,\n"
            "                       text: elements.get('live-text').textContent});\n"
            "  await flush();\n"
            "  const first = snapshotPage();\n"
            "  fakeNow += 5000;\n"
            "  refresh();\n"
            "  await flush();\n"
            "  const lost = live();\n"
            "  const after = snapshotPage();\n"
            "  fakeNow += 3000;\n"
            "  renderLive();\n"
            "  console.log(JSON.stringify({first, lost, after, later: live()}));\n"
            "})();\n",
            responses=[
                {"ok": True, "status": 200, "data": FULL},
                {"ok": True, "status": 200, "data": body},
            ],
        )
        lost = {"dot": "dot serious", "text": "Connection lost · showing last data"}
        assert output["lost"] == lost
        assert output["later"] == lost
        for key, node in output["first"].items():
            if key not in ("live-dot", "live-text"):
                assert output["after"][key] == node, key

    def test_non_object_first_body_says_retrying(self):
        output = _execute(
            "(async () => {\n"
            "  await flush();\n"
            "  console.log(JSON.stringify({dot: elements.get('live-dot').className,\n"
            "    text: elements.get('live-text').textContent}));\n"
            "})();\n",
            responses=[{"ok": True, "status": 200, "data": None}],
        )
        assert output == {"dot": "dot serious", "text": "Connection lost · retrying"}

    def test_failure_before_any_data_says_retrying(self):
        output = _execute(
            "(async () => {\n"
            "  await flush();\n"
            "  console.log(JSON.stringify({dot: elements.get('live-dot').className,\n"
            "    text: elements.get('live-text').textContent,\n"
            "    rows: elements.get('models').children.length}));\n"
            "})();\n",
        )
        assert output == {
            "dot": "dot serious",
            "text": "Connection lost · retrying",
            "rows": 0,
        }
