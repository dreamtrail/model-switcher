"""Static and rendering tests for the ``/stats`` dashboard page."""

from __future__ import annotations

import json
import re

import pytest

from src.stats import STATS_HTML
from tests.stats_page_helpers import (
    FULL,
    FRESH,
    HOSTILE,
    _all_text,
    _cells,
    _classes,
    _execute,
    _has_meter,
    _lines,
    _page_ids,
    _pill,
    _render,
    _rows,
    _text,
)


class TestStatsPageStatic:
    def test_page_renders_text_only_without_innerhtml_or_external_assets(self):
        assert "innerHTML" not in STATS_HTML
        assert "outerHTML" not in STATS_HTML
        assert "insertAdjacentHTML" not in STATS_HTML
        assert "document.write" not in STATS_HTML
        assert ".textContent" in STATS_HTML
        assert 'fetch("/stats/json")' in STATS_HTML
        assert "setInterval(refresh, 5000)" in STATS_HTML
        assert '<a href="/stats/json">/stats/json</a>' in STATS_HTML
        assert "http://" not in STATS_HTML and "https://" not in STATS_HTML
        assert "<title>Model Switcher — Statistics</title>" in STATS_HTML
        assert "<noscript>" in STATS_HTML

    def test_header_links_to_control_panel(self):
        header = STATS_HTML.split('<header class="top">', 1)[1].split("</header>", 1)[0]
        assert '<p class="subtitle">Statistics · <a href="/control">Control panel</a></p>' in header
        assert ".subtitle a" in STATS_HTML

    def test_table_has_the_thirteen_compact_columns(self):
        head = STATS_HTML.split("<thead>", 1)[1].split("</thead>", 1)[0]
        cells = re.findall(r"<th([^>]*)>([^<]*)</th>", head)
        assert [label for _, label in cells] == [
            "Model", "Reqs", "Errors", "Gen t/s", "Live t/s ≈", "Gen total",
            "Prompt t/s", "Prompt total", "Prompt share", "TTFT p50 / p95",
            "Cache", "Gen tok", "Prefill tok",
        ]
        tip = ' title="Combined speed while busy @ average concurrent requests"'
        live = ' title="Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)"'
        prompt = ' title="Prompt processing speed; uses the server\'s own prefill time when available, else time to first token"'
        gen = ' title="Generation speed; uses the server\'s own decode time when available, else first to last streamed chunk"'
        share = ' title="Share of request time spent on prompt processing (the rest is generation); counts only requests where both times are known"'
        assert [attrs for attrs, _ in cells] == [
            "", "", "", gen, live, tip, prompt, tip, share, "", "", "", "",
        ]
        assert "Best gen t/s" not in STATS_HTML
        assert '<tbody id="models"></tbody>' in STATS_HTML
        assert '<tfoot id="totals"></tfoot>' in STATS_HTML

    def test_palette_uses_custom_properties_with_dark_mode(self):
        style = STATS_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        light, dark = style.split("@media (prefers-color-scheme: dark)", 1)
        for prop in (
            "--plane: #f9f9f7", "--surface: #fcfcfb", "--ink: #0b0b0b",
            "--ink-2: #52514e", "--muted: #898781", "--hairline: #e1e0d9",
            "--baseline: #c3c2b7", "--accent: #2a78d6",
            "--good: #0ca30c", "--serious: #ec835a", "--critical: #d03b3b",
        ):
            assert prop in light
        for prop in (
            "--plane: #0d0d0d", "--surface: #1a1a19", "--ink: #ffffff",
            "--ink-2: #c3c2b7", "--hairline: #2c2c2a", "--baseline: #383835",
            "--accent: #3987e5",
        ):
            assert prop in dark
        # The cache hit-rate meter and its track colour are gone.
        assert "--track" not in style
        assert ".meter" not in style
        assert ".pct" not in style
        # Tabular figures only in the table; stat tiles use proportional ones.
        assert "font-variant-numeric: tabular-nums" in style
        assert "font-variant-numeric: proportional-nums" in style
        # The totals separator must survive the table border model.
        assert "border-collapse: separate" in style
        assert "tfoot td { border-top: 1px solid var(--baseline)" in style

    def test_initial_markup_hides_conditional_parts(self):
        hidden = {item["id"] for item in _page_ids() if item["hidden"]}
        assert {"errors-card", "kpi-model-dot", "kpi-gen-unit"} <= hidden
        assert '<span id="live-text">Connecting…</span>' in STATS_HTML


class TestStatsPageRendering:
    def test_full_data_renders_tiles(self):
        page = _render(FULL)

        assert _text(page, "kpi-model") == "qwen3-32b"
        assert page["kpi-model"]["title"] == "qwen3-32b"
        assert "idle" not in page["kpi-model"]["className"]
        assert page["kpi-model-dot"]["hidden"] is False
        assert _text(page, "kpi-model-sub") == "Serving"
        assert _text(page, "kpi-gen") == "52.7"
        assert page["kpi-gen-unit"]["hidden"] is False
        assert _text(page, "kpi-live") == "38.3"
        assert page["kpi-live"]["className"] == "value"
        assert _text(page, "kpi-reqs") == "1,400"
        assert page["kpi-reqs"]["title"] == "1,400"
        assert _lines(page, "kpi-reqs-sub") == ["2 errors", "7 in last min"]
        assert _text(page, "kpi-active") == "1"
        assert _text(page, "kpi-active-sub") == "peak 4"
        assert _text(page, "kpi-loads") == "15"
        assert _lines(page, "kpi-loads-sub") == ["avg 41.2 s", "3 failed"]
        assert page["kpi-loads-tile"]["title"] == (
            "Model loads: process start until the health check passes."
            " Swaps: 12. Failed: 3."
            " Avg stop of the previous model: 2.3 s (not included in the average)."
        )
        assert _text(page, "kpi-uptime") == "3h 12m"
        assert _lines(page, "kpi-uptime-sub") == ["busy 1h 4m", "33% of uptime"]

    def test_full_data_renders_models_table_with_pill_and_totals(self):
        page = _render(FULL)

        rows = _rows(page)
        assert [_cells(row)[0] for row in rows] == [
            "qwen3-32b", "big", "aaa", "llama-3.1-8b",
        ]
        assert [row["className"] for row in rows] == ["loaded", "", "", ""]
        pill = _pill(rows[0])
        assert pill["className"] == "pill"
        assert pill["text"] == "Loaded"
        assert _classes(pill) == ["dot good", ""]
        assert all(_pill(row) is None for row in rows[1:])

        assert _cells(rows[0]) == [
            "qwen3-32b", "312", "2", "42.1", "38.3", "85.3 @2.4", "1,830", "2,140 @1.0",
            "20%", "210 / 890 ms", "64%", "45.2K", "9,876",
        ]
        assert _cells(rows[3]) == [
            "llama-3.1-8b", "88", "0", "96.4", "–", "–", "4,120", "–",
            "–", "95 / 310 ms", "31%", "0", "0",
        ]
        assert _cells(rows[1]) == [
            "big", "1,000", "–", "–", "–", "–", "–", "–", "–", "–", "–", "–", "–",
        ]
        # Token counts compact like the tiles, full value on hover.
        assert rows[0]["children"][11]["title"] == "45,210"
        assert rows[0]["children"][12]["title"] == "9,876"
        assert rows[1]["children"][11]["title"] == ""
        assert rows[1]["children"][11]["className"] == "muted"
        # Prompt share: percentage, seconds on hover; muted dash when null.
        assert rows[0]["children"][8]["title"] == (
            "Prompt 0.4 s · Gen 1.6 s over 1 request"
        )
        assert rows[0]["children"][8]["className"] == ""
        assert rows[3]["children"][8]["title"] == ""
        assert rows[3]["children"][8]["className"] == "muted"
        assert rows[1]["children"][8]["title"] == ""

        # Errors: plain count (no dot) when > 0; zero is muted.
        assert _classes(rows[0]["children"][2]) == [""]
        assert _classes(rows[3]["children"][2]) == ["muted"]
        # TTFT: numbers in primary ink, unit muted.
        assert _classes(rows[0]["children"][9]) == ["", "muted"]
        assert rows[0]["children"][9]["children"][1]["text"] == " ms"
        # Cache: plain percentage text, no meter bar; muted dash when null.
        for row in rows:
            assert not _has_meter(row["children"][10])
        assert rows[0]["children"][10]["className"] == ""
        assert rows[1]["children"][10]["className"] == "muted"

        totals = _rows(page, "totals")
        assert len(totals) == 1
        assert totals[0]["className"] == "all"
        assert _pill(totals[0]) is None
        # The All models row uses the global aggregates and token totals.
        assert _cells(totals[0]) == [
            "All models", "1,400", "2", "52.7", "38.3", "77.7 @2.1", "2,204", "3,140 @1.8",
            "62%", "180 / 850 ms", "58%", "1.23M", "9,876",
        ]
        assert totals[0]["children"][11]["title"] == "1,234,567"
        assert totals[0]["children"][8]["title"] == (
            "Prompt 1,234.6 s · Gen 756.0 s over 1,398 requests"
        )
        assert not _has_meter(totals[0]["children"][10])

    def test_full_data_shows_errors_card_with_chips(self):
        page = _render(FULL)

        assert page["errors-card"]["hidden"] is False
        chips = page["errors"]["children"]
        assert [chip["className"] for chip in chips] == ["chip"] * 3
        assert [[c["text"] for c in chip["children"]] for chip in chips] == [
            ["", "stream_error", "3"],
            ["", "upstream_timeout", "3"],
            ["", "502", "1"],
        ]
        assert all(_classes(chip)[0] == "dot critical" for chip in chips)

    def test_fresh_data_shows_idle_tiles_only_totals_and_hides_errors(self):
        page = _render(FRESH)

        assert _text(page, "kpi-model") == "None"
        assert "idle" in page["kpi-model"]["className"]
        assert page["kpi-model-dot"]["hidden"] is True
        assert _text(page, "kpi-model-sub") == "Idle"
        assert _text(page, "kpi-gen") == "–"
        assert page["kpi-gen-unit"]["hidden"] is True
        assert _text(page, "kpi-live") == "–"
        assert page["kpi-live"]["className"] == "value idle"
        assert _text(page, "kpi-reqs") == "0"
        assert _lines(page, "kpi-reqs-sub") == ["0 errors", "0 in last min"]
        assert _text(page, "kpi-active") == "0"
        assert _text(page, "kpi-active-sub") == "peak 0"
        assert _text(page, "kpi-loads") == "0"
        assert _lines(page, "kpi-loads-sub") == ["avg –"]
        assert page["kpi-loads-tile"]["title"] == (
            "Model loads: process start until the health check passes."
        )
        assert _text(page, "kpi-uptime") == "5s"
        assert _lines(page, "kpi-uptime-sub") == ["busy 0s", "0% of uptime"]

        assert _rows(page) == []
        totals = _rows(page, "totals")
        assert _cells(totals[0]) == [
            "All models", "0", "0", "–", "–", "–", "–", "–", "–", "–", "–", "–", "–",
        ]
        assert not _has_meter(totals[0]["children"][10])
        assert page["errors-card"]["hidden"] is True
        assert page["errors"]["children"] == []

    def test_single_failure_uses_singular_error(self):
        page = _render({"requests": {"total": 3, "failure": 1}, "throughput": {"last_60s": 1}})
        assert _lines(page, "kpi-reqs-sub") == ["1 error", "1 in last min"]

    def test_requests_sub_line_has_no_separator_to_dangle(self):
        page = _render(FULL)
        assert "·" not in _text(page, "kpi-reqs-sub")
        assert len(page["kpi-reqs-sub"]["children"]) == 2

    @pytest.mark.parametrize(
        ("loads", "lines"),
        [
            ({"count": 1, "avg_ms": 41234.0, "swaps": 0, "failed": {}},
             ["avg 41.2 s"]),
            ({"count": 2, "avg_ms": 900.0, "swaps": 1, "failed": {}},
             ["avg 0.9 s", "1 swap"]),
            ({"count": 4, "avg_ms": 1000.0, "swaps": 3, "failed": {}},
             ["avg 1.0 s", "3 swaps"]),
            ({"count": 0, "avg_ms": None, "swaps": 0,
              "failed": {"model_start_failed": 1}},
             ["avg –", "1 failed"]),
            ({"count": 5, "avg_ms": 2000.0, "swaps": 3,
              "failed": {"swap_timeout": 1, "model_start_failed": 1},
              "abandoned": {"swap_superseded": 7}},
             ["avg 2.0 s", "2 failed"]),
            ({"count": 1, "avg_ms": 2000.0, "swaps": 0, "failed": {},
              "abandoned": {"cancelled": 2}},
             ["avg 2.0 s"]),
        ],
    )
    def test_loads_sub_lines(self, loads, lines):
        page = _render({"loads": loads})
        assert _lines(page, "kpi-loads-sub") == lines
        assert "·" not in lines[0]

    @pytest.mark.parametrize(
        ("loads", "extra"),
        [
            ({"count": 1, "swaps": 0, "failed": {},
              "stop": {"count": 0, "avg_ms": None}}, ""),
            ({"count": 2, "swaps": 1, "failed": {},
              "stop": {"count": 1, "avg_ms": 12345.0}},
             " Swaps: 1. Avg stop of the previous model: 12.3 s"
             " (not included in the average)."),
            ({"count": 0, "swaps": 0, "failed": {"swap_timeout": 2},
              "stop": {"count": 0, "avg_ms": None}},
             " Failed: 2."),
            ({"count": 1, "swaps": "3", "failed": [],
              "stop": {"count": 1, "avg_ms": float("nan")}}, ""),
        ],
    )
    def test_loads_tooltip_lists_only_nonzero_parts(self, loads, extra):
        base = "Model loads: process start until the health check passes."
        payload = json.dumps({"loads": loads}, allow_nan=True)
        page = _execute("render(" + payload + ");\n"
                        "console.log(JSON.stringify(snapshotPage()));\n")
        assert page["kpi-loads-tile"]["title"] == base + extra

    def test_loads_tile_replaces_swaps_tile(self):
        assert '<div class="label">Loads</div>' in STATS_HTML
        assert '<div class="label">Swaps</div>' not in STATS_HTML
        assert "kpi-swaps" not in STATS_HTML
        ids = [item["id"] for item in _page_ids()]
        assert ids.count("kpi-loads") == ids.count("kpi-loads-sub") == 1
        assert STATS_HTML.count('class="card tile') == 7

    @pytest.mark.parametrize(
        ("value", "shown", "title"),
        [
            (0, "0", "0"),
            (1234, "1,234", "1,234"),
            (9999, "9,999", "9,999"),
            (9999.6, "10.0K", "10,000"),
            (10000, "10.0K", "10,000"),
            (12345, "12.3K", "12,345"),
            (999949, "999.9K", "999,949"),
            (999950, "1.00M", "999,950"),
            (1234567, "1.23M", "1,234,567"),
            (999994999, "999.99M", "999,994,999"),
            (1234567890, "1.23B", "1,234,567,890"),
            (-12345, "-12.3K", "-12,345"),
        ],
    )
    def test_tile_numbers_compact_with_full_value_in_title(self, value, shown, title):
        page = _render({
            "requests": {"total": value},
            "concurrency": {"active": value},
            "loads": {"count": value},
        })
        for ident in ("kpi-reqs", "kpi-active", "kpi-loads"):
            assert _text(page, ident) == shown, ident
            assert page[ident]["title"] == title, ident

    @pytest.mark.parametrize("value", [None, "12345", float("nan"), [], {}])
    def test_tile_numbers_missing_show_dash_without_title(self, value):
        payload = json.dumps({
            "requests": {"total": value},
            "concurrency": {"active": value},
            "loads": {"count": value},
        }, allow_nan=True)
        page = _execute("render(" + payload + ");\n"
                        "console.log(JSON.stringify(snapshotPage()));\n")
        for ident in ("kpi-reqs", "kpi-active", "kpi-loads"):
            assert _text(page, ident) == "–", ident
            assert page[ident]["title"] == "", ident

    def test_table_cells_keep_full_numbers(self):
        page = _render({
            "requests": {"total": 1234567},
            "models": {"m": {"requests": {"total": 1234567}}},
        })
        assert _text(page, "kpi-reqs") == "1.23M"
        assert _cells(_rows(page)[0])[1] == "1,234,567"
        assert _cells(_rows(page, "totals")[0])[1] == "1,234,567"

    @pytest.mark.parametrize(
        ("rate", "text"),
        [(1.7, "170%"), (1, "100%"), (0, "0%"), (-0.2, "-20%"), (0.5049, "50%")],
    )
    def test_cache_shows_percentage_without_meter(self, rate, text):
        page = _render({"models": {"m": {"requests": {"tokens": {"cache_hit_rate": rate}}}}})
        cell = _rows(page)[0]["children"][10]
        assert cell["text"] == text
        assert cell["className"] == ""
        assert not _has_meter(cell)

    @pytest.mark.parametrize("rate", [None, "0.5", [], {}])
    def test_cache_without_numeric_rate_shows_muted_dash(self, rate):
        page = _render({"models": {"m": {"requests": {"tokens": {"cache_hit_rate": rate}}}}})
        cell = _rows(page)[0]["children"][10]
        assert cell["text"] == "–"
        assert cell["className"] == "muted"
        assert not _has_meter(cell)

    @pytest.mark.parametrize(
        ("split", "text", "title"),
        [
            (
                {"prefill_s": 123.44, "decode_s": 75.6, "prefill_share": 0.62,
                 "generation_share": 0.38, "requests": 42},
                "62%", "Prompt 123.4 s · Gen 75.6 s over 42 requests",
            ),
            (
                {"prefill_s": 0.25, "decode_s": 4.75, "prefill_share": 0.05,
                 "generation_share": 0.95, "requests": 1},
                "5%", "Prompt 0.3 s · Gen 4.8 s over 1 request",
            ),
            (
                {"prefill_s": 3.0, "decode_s": 0.0, "prefill_share": 1.0,
                 "generation_share": 0.0, "requests": 2},
                "100%", "Prompt 3.0 s · Gen 0.0 s over 2 requests",
            ),
            # Share known but seconds missing: percentage without tooltip.
            ({"prefill_share": 0.5}, "50%", ""),
        ],
    )
    def test_prompt_share_percentage_and_seconds_tooltip(self, split, text, title):
        page = _render({"models": {"m": {"requests": {"time_split": split}}}})
        cell = _rows(page)[0]["children"][8]
        assert cell["text"] == text
        assert cell["title"] == title
        assert cell["className"] == ""

    @pytest.mark.parametrize(
        "split",
        [
            None, [], {}, "0.5",
            {"prefill_s": 0.0, "decode_s": 0.0, "prefill_share": None,
             "generation_share": None, "requests": 0},
            {"prefill_s": 1.0, "decode_s": 1.0, "prefill_share": "0.5",
             "requests": 1},
        ],
    )
    def test_prompt_share_without_numeric_share_shows_muted_dash(self, split):
        page = _render({"models": {"m": {"requests": {"time_split": split}}}})
        cell = _rows(page)[0]["children"][8]
        assert cell["text"] == "–"
        assert cell["title"] == ""
        assert cell["className"] == "muted"

    @pytest.mark.parametrize(
        ("value_expression", "rate", "expected"),
        [
            ("{tps: 85.34, concurrency: 2.36}", "fmtTps", "85.3 @2.4"),
            ("{tps: 85.34, concurrency: 1}", "fmtTps", "85.3 @1.0"),
            ("{tps: 2140.4, concurrency: 1.84}", "fmtInt", "2,140 @1.8"),
            ("null", "fmtTps", "–"),
            ("42", "fmtTps", "–"),
            ('"primitive"', "fmtTps", "–"),
            ("[]", "fmtTps", "–"),
            ("{tps: NaN, concurrency: 1}", "fmtTps", "–"),
            ("{tps: Infinity, concurrency: 1}", "fmtTps", "–"),
            ("{tps: 0, concurrency: 1}", "fmtTps", "–"),
            ("{tps: -1, concurrency: 1}", "fmtTps", "–"),
            ('{tps: "5", concurrency: 1}', "fmtTps", "–"),
            ("{concurrency: 1}", "fmtTps", "–"),
            ("{tps: 1}", "fmtTps", "–"),
            ("{tps: 1, concurrency: 0}", "fmtTps", "–"),
            ("{tps: 1, concurrency: -Infinity}", "fmtTps", "–"),
            ("{tps: 1, concurrency: NaN}", "fmtInt", "–"),
        ],
    )
    def test_aggregate_formatter_contract(self, value_expression, rate, expected):
        output = _execute(
            "console.log(JSON.stringify(fmtAggregate("
            + value_expression + ", " + rate + ")));\n"
        )
        assert output == expected

    def test_errors_card_hides_again_after_errors_clear(self):
        output = _execute(
            "render(" + json.dumps(FULL) + ");\n"
            "const shown = snapshotPage();\n"
            "render(" + json.dumps(FRESH) + ");\n"
            "console.log(JSON.stringify({shown, cleared: snapshotPage()}));\n"
        )
        assert output["shown"]["errors-card"]["hidden"] is False
        assert output["cleared"]["errors-card"]["hidden"] is True
        assert output["cleared"]["errors"]["children"] == []
        # Re-rendering replaces rows instead of appending.
        assert output["cleared"]["models"]["children"] == []
        assert len(output["cleared"]["totals"]["children"]) == 1

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"models": None, "requests": None, "loads": None, "concurrency": None},
            {"models": [], "requests": [], "throughput": "x", "uptime_seconds": "5"},
            {"models": {"m": None, "n": {"requests": None}, "o": {"requests": {"rates": None, "tokens": 3}}}},
            {
                "models": {"m": {"requests": {
                    "total": float("nan"),
                    "failure": "2",
                    "rates": {
                        "generation_tps": {"avg": "fast", "avg_aggregate": {"tps": float("nan"), "concurrency": 1}},
                        "prompt_tps": {"avg_aggregate": {"tps": 5, "concurrency": float("inf")}},
                    },
                    "ttft_ms": {"p50": None, "p95": 12.6},
                    "tokens": {"cache_hit_rate": float("inf"), "completion": float("nan"), "prefill": "9"},
                }}},
                "requests": {"errors": {"x": "many", "y": None, "z": -1}, "failure": float("nan")},
                "uptime_seconds": -3,
                "loads": {
                    "count": "3", "avg_ms": float("-inf"), "swaps": "2",
                    "stop": {"count": 1, "avg_ms": float("nan")},
                    "failed": {"x": "many", "y": None, "z": -1},
                    "abandoned": [],
                },
            },
        ],
    )
    def test_missing_or_malformed_fields_do_not_throw(self, data):
        # NaN/Infinity are not valid JSON but are valid JS literals.
        payload = json.dumps(data, allow_nan=True)
        output = _execute(
            "render(" + payload + ");\n"
            "console.log(JSON.stringify(snapshotPage()));\n"
        )
        totals = _rows(output, "totals")
        assert _cells(totals[0])[0] == "All models"
        assert all(len(row["children"]) == 13 for row in _rows(output) + totals)
        assert output["errors-card"]["hidden"] is True
        for node in output.values():
            for text in _all_text(node):
                assert "NaN" not in text
                assert "undefined" not in text
                assert "null" not in text
                assert "Infinity" not in text
                assert "[object" not in text

    def test_malformed_values_render_as_dashes(self):
        page = _render({
            "current_model": "m",
            "models": {"m": {"requests": {
                "total": 3,
                "rates": {
                    "generation_tps": {"avg": "fast", "avg_aggregate": {"tps": "5", "concurrency": 1}},
                    "prompt_tps": {"avg_aggregate": [5, 1]},
                },
                "ttft_ms": {"p50": None, "p95": 12.6},
                "tokens": {"completion": None, "prefill": {}},
            }}},
        })
        row = _rows(page)[0]
        assert _cells(row) == [
            "m", "3", "–", "–", "–", "–", "–", "–", "–", "– / 13 ms", "–", "–", "–",
        ]
        assert row["className"] == "loaded"
        assert _pill(row)["text"] == "Loaded"

    @pytest.mark.parametrize(
        ("name", "tile_class"),
        [
            ("qwen3-32b", "value name"),
            ("a" * 12, "value name"),
            ("a" * 13, "value name long"),
            ("Meta-Llama-3.1-70B-Instruct-Q4_K_M-long-context-128k", "value name long"),
        ],
    )
    def test_long_model_names_keep_full_text_and_title(self, name, tile_class):
        page = _render({
            "current_model": name,
            "models": {name: {"requests": {"total": 1}}},
        })
        # Loaded tile: long names switch to the smaller wrapping style.
        assert page["kpi-model"]["className"] == tile_class
        assert _text(page, "kpi-model") == name
        assert page["kpi-model"]["title"] == name
        # Table: the name span is capped by CSS; the full name stays in text + title,
        # and the Loaded pill is a separate sibling so it is never truncated.
        row = _rows(page)[0]
        label = row["children"][0]["children"][0]
        assert label["className"] == "model-name"
        assert label["text"] == name
        assert label["title"] == name
        assert _pill(row)["text"] == "Loaded"

    def test_name_cap_styles_present(self):
        style = STATS_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "max-width: 12rem" in style
        assert "-webkit-line-clamp: 2" in style
        assert ".tile .value.name.long" in style

    def test_hostile_model_and_error_names_render_as_literal_text(self):
        page = _render({
            "current_model": HOSTILE,
            "models": {HOSTILE: {"requests": {"total": 1, "failure": 1}}},
            "requests": {"total": 1, "failure": 1, "errors": {HOSTILE: 1}},
        })
        assert _text(page, "kpi-model") == HOSTILE
        assert page["kpi-model"]["title"] == HOSTILE
        row = _rows(page)[0]
        assert _cells(row)[0] == HOSTILE
        assert row["children"][0]["children"][0]["title"] == HOSTILE
        assert row["className"] == "loaded"
        assert _pill(row) is not None
        chip = page["errors"]["children"][0]
        assert [c["text"] for c in chip["children"]] == ["", HOSTILE, "1"]
        # Only elements created by the page script exist: no parsed markup.
        def tags(node):
            yield node["tag"]
            for child in node["children"]:
                yield from tags(child)
        created = {t for node in page.values() for t in tags(node)}
        assert created <= {"div", "tr", "td", "span"}
