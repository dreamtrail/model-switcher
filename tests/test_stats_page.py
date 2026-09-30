"""Executable JavaScript tests for the ``/stats`` dashboard page."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from src.stats import STATS_HTML


HOSTILE = "<img src=x onerror=alert(1)>"

FULL = {
    "uptime_seconds": 11520.7,
    "current_model": "qwen3-32b",
    "concurrency": {"active": 1, "peak": 4},
    "requests": {
        "total": 1400,
        "success": 1398,
        "failure": 2,
        "errors": {"502": 1, "stream_error": 3, "upstream_timeout": 3},
        "tokens": {"cache_hit_rate": 0.584, "completion": 1234567, "prefill": 9876},
        "rates": {
            "prompt_tps": {
                "avg": 2204.4,
                "avg_aggregate": {"tps": 3140.46, "concurrency": 1.84},
            },
            "generation_tps": {
                "avg": 52.66,
                "avg_aggregate": {"tps": 77.71, "concurrency": 2.14},
                "live": {"tps": 38.25, "window_s": 15, "estimate": True},
            },
        },
        "ttft_ms": {"p50": 180.2, "p95": 849.6},
        "time_split": {
            "prefill_s": 1234.56, "decode_s": 756.04,
            "prefill_share": 0.6202, "generation_share": 0.3798,
            "requests": 1398,
        },
    },
    "throughput": {"last_60s": 7, "last_300s": 20, "avg_rpm": 1.2},
    "swaps": {"count": 12, "avg_ms": 38210.0, "p95_ms": 50000.0, "max_ms": 51000.0},
    "utilization": {"busy_seconds": 3845.2, "busy_share": 0.3338},
    "models": {
        "llama-3.1-8b": {
            "starts": 3,
            "stops": 3,
            "requests": {
                "total": 88,
                "failure": 0,
                "tokens": {"cache_hit_rate": 0.31, "completion": 0, "prefill": 0},
                "rates": {
                    "prompt_tps": {"avg": 4120.0, "avg_aggregate": None},
                    "generation_tps": {
                        "avg": 96.44, "max_aggregate": None, "avg_aggregate": None,
                        "live": {"tps": 0.0, "window_s": 15, "estimate": True},
                    },
                },
                "ttft_ms": {"p50": 95.0, "p95": 310.4},
                "time_split": {
                    "prefill_s": 0.0, "decode_s": 0.0,
                    "prefill_share": None, "generation_share": None,
                    "requests": 0,
                },
            },
        },
        "qwen3-32b": {
            "starts": 4,
            "stops": 3,
            "requests": {
                "total": 312,
                "failure": 2,
                "tokens": {"cache_hit_rate": 0.64, "completion": 45210, "prefill": 9876},
                "rates": {
                    "prompt_tps": {
                        "avg": 1830.2,
                        "avg_aggregate": {"tps": 2140.2, "concurrency": 1.0},
                    },
                    "generation_tps": {
                        "avg": 42.1,
                        "max_aggregate": {"tps": 118.44, "concurrency": 4},
                        "avg_aggregate": {"tps": 85.34, "concurrency": 2.36},
                        "live": {"tps": 38.25, "window_s": 15, "estimate": True},
                    },
                },
                "ttft_ms": {"p50": 210.4, "p95": 890.0},
                "time_split": {
                    "prefill_s": 0.4, "decode_s": 1.6,
                    "prefill_share": 0.2, "generation_share": 0.8,
                    "requests": 1,
                },
            },
        },
        "big": {"requests": {"total": 1000}},
        "aaa": {"requests": {"total": 88}},
    },
}

FRESH = {
    "uptime_seconds": 5.2,
    "current_model": None,
    "concurrency": {"active": 0, "peak": 0},
    "requests": {
        "total": 0,
        "success": 0,
        "failure": 0,
        "success_rate": None,
        "errors": {},
        "latency_ms": None,
        "tokens": {"cache_hit_rate": None},
        "rates": {"prompt_tps": {"avg": None}, "generation_tps": {"avg": None}},
        "ttft_ms": None,
    },
    "throughput": {"last_60s": 0, "last_300s": 0, "avg_rpm": 0.0},
    "swaps": {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None},
    "utilization": {"busy_seconds": 0.0, "busy_share": 0.0},
    "models": {},
}


def _page_ids() -> list[dict]:
    """Every element id in the page markup, with its initial ``hidden`` flag."""
    ids = []
    for tag in re.findall(r"<[a-z][^>]*\bid=\"[^\"]+\"[^>]*>", STATS_HTML):
        ident = re.search(r"\bid=\"([^\"]+)\"", tag).group(1)
        ids.append({"id": ident, "hidden": bool(re.search(r"\shidden[\s>]", tag))})
    return ids


HARNESS = r'''
class Element {
  constructor(tagName, id = null) {
    this.tagName = tagName;
    this.id = id;
    this.children = [];
    this._textContent = "";
    this.className = "";
    this.hidden = false;
    this.title = "";
    this.style = {};
    this.attributes = {};
  }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent).join("")
      : this._textContent;
  }
  set innerHTML(value) { throw new Error("innerHTML used"); }
  set outerHTML(value) { throw new Error("outerHTML used"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML used"); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  appendChild(child) { this.children.push(child); return child; }
}
const elements = new Map(
  PAGE_IDS.map((item) => {
    const node = new Element("div", item.id);
    node.hidden = item.hidden;
    return [item.id, node];
  })
);
const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement(tagName) { return new Element(tagName); },
  write() { throw new Error("document.write used"); },
};
let fakeNow = 1000000;
Date.now = () => fakeNow;
const responseQueue = RESPONSES;
const fetchCalls = [];
function fetch(url) {
  fetchCalls.push(url);
  if (!responseQueue.length) return Promise.reject(new Error("offline"));
  const item = responseQueue.shift();
  return Promise.resolve({
    ok: item.ok,
    status: item.status,
    json() { return Promise.resolve(item.data); },
  });
}
const intervals = [];
function setInterval(fn, ms) { intervals.push({name: fn.name, ms}); return intervals.length; }
async function flush() {
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}
function dump(node) {
  return {
    tag: node.tagName,
    className: node.className,
    text: node.textContent,
    hidden: node.hidden,
    title: node.title,
    width: node.style.width === undefined ? null : node.style.width,
    attributes: node.attributes,
    children: node.children.map(dump),
  };
}
function byIdDump(id) { return dump(elements.get(id)); }
function snapshotPage() {
  const out = {};
  for (const id of elements.keys()) out[id] = byIdDump(id);
  return out;
}
'''


def _script() -> str:
    return STATS_HTML.split("<script>", 1)[1].split("</script>", 1)[0]


def _execute(expression: str, responses: list[dict] | None = None):
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to execute the embedded stats page JavaScript")
    harness = (
        HARNESS.replace("PAGE_IDS", json.dumps(_page_ids()))
        .replace("RESPONSES", json.dumps(responses or []))
    )
    completed = subprocess.run(
        [node, "-"],
        input=harness + _script() + "\n" + expression,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _render(data) -> dict:
    return _execute(
        "render(" + json.dumps(data) + ");\n"
        "console.log(JSON.stringify(snapshotPage()));\n"
    )


def _text(page, ident) -> str:
    return page[ident]["text"]


def _rows(page, ident="models") -> list[dict]:
    return page[ident]["children"]


def _cells(row) -> list[str]:
    """Visible cell texts; the model cell reports only the name, not the pill."""
    tds = row["children"]
    return [tds[0]["children"][0]["text"]] + [td["text"] for td in tds[1:]]


def _pill(row):
    extra = row["children"][0]["children"][1:]
    return extra[0] if extra else None


def _classes(node) -> list[str]:
    return [child["className"] for child in node["children"]]


def _has_meter(td) -> bool:
    """True when any descendant is the removed cache-rate meter bar."""
    return any(
        c["className"] in ("meter", "fill") or _has_meter(c)
        for c in td["children"]
    )


def _lines(page, ident) -> list[str]:
    """Texts of a sub-line's stacked line spans (each its own block)."""
    children = page[ident]["children"]
    assert [c["className"] for c in children] == ["line"] * len(children)
    return [c["text"] for c in children]


def _all_text(node) -> list[str]:
    texts = [node["text"], node["title"]]
    for child in node["children"]:
        texts.extend(_all_text(child))
    return texts


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
        assert _text(page, "kpi-swaps") == "12"
        assert _text(page, "kpi-swaps-sub") == "avg 38.2 s"
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

        # Errors: critical dot only when > 0; zero is muted.
        assert _classes(rows[0]["children"][2]) == ["dot critical", ""]
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
        assert _text(page, "kpi-swaps") == "0"
        assert _text(page, "kpi-swaps-sub") == "avg –"
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
            "swaps": {"count": value},
        })
        for ident in ("kpi-reqs", "kpi-active", "kpi-swaps"):
            assert _text(page, ident) == shown, ident
            assert page[ident]["title"] == title, ident

    @pytest.mark.parametrize("value", [None, "12345", float("nan"), [], {}])
    def test_tile_numbers_missing_show_dash_without_title(self, value):
        payload = json.dumps({
            "requests": {"total": value},
            "concurrency": {"active": value},
            "swaps": {"count": value},
        }, allow_nan=True)
        page = _execute("render(" + payload + ");\n"
                        "console.log(JSON.stringify(snapshotPage()));\n")
        for ident in ("kpi-reqs", "kpi-active", "kpi-swaps"):
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
            {"models": None, "requests": None, "swaps": None, "concurrency": None},
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
                "swaps": {"count": "3", "avg_ms": float("-inf")},
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


class TestStatsPageLiveRate:
    """P-20260926-01: estimated live generation rate tile and column.

    Tooltip text updated by P-20260926-04 (busy-time, calibrated).
    """

    TIP = "Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)"

    def test_tile_markup_is_labelled_as_estimate_with_tooltip(self):
        tiles = STATS_HTML.split('<section class="kpis"', 1)[1].split("</section>", 1)[0]
        assert f'<div class="card tile" title="{self.TIP}">' in tiles
        assert '<div class="label">Live gen t/s ≈</div>' in tiles
        assert '<div id="kpi-live" class="value">–</div>' in tiles
        assert "estimate · 15 s" in tiles
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
            ("{tps: 12.34, window_s: 15, estimate: true}", "12.3"),
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

    def test_live_values_render_per_model_and_in_all_models_row(self):
        live = lambda tps: {"tps": tps, "window_s": 15, "estimate": True}
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
