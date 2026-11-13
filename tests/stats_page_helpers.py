"""Shared data and JavaScript execution helpers for the ``/stats`` page."""

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
                "live": {"tps": 38.25, "window_s": 5, "estimate": True},
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
    "loads": {
        "count": 15, "avg_ms": 41234.0, "p95_ms": 52000.0, "max_ms": 53000.0,
        "swaps": 12,
        "stop": {"count": 12, "avg_ms": 2345.0, "p95_ms": 4000.0, "max_ms": 4100.0},
        "failed": {"swap_timeout": 1, "model_start_failed": 2},
        "abandoned": {"swap_superseded": 4},
    },
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
                        "live": {"tps": 0.0, "window_s": 5, "estimate": True},
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
                        "live": {"tps": 38.25, "window_s": 5, "estimate": True},
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
    "loads": {
        "count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None,
        "swaps": 0,
        "stop": {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None},
        "failed": {}, "abandoned": {},
    },
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
