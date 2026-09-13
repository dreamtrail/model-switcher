"""In-memory runtime statistics collection for the switcher."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

# Trailing windows for throughput reporting.
WINDOW_SHORT_S = 60
WINDOW_LONG_S = 300
# Request timestamps older than this are pruned from the deque.
REQUEST_RETENTION_S = 3600


@dataclass
class Outcome:
    """Mutable stream outcome reported back by the proxy functions.

    proxy.py writes to this object; it never touches StatsCollector.
    """

    stream_error: bool = False
    bytes_out: int = 0


@dataclass
class _ModelStats:
    """Per-model counters and samples."""

    starts: int = 0
    stops: int = 0
    total: int = 0
    success: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)  # seconds
    bytes_in: int = 0
    bytes_out: int = 0


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending-sorted sample list.

    Returns sorted_samples[ceil(q * n) - 1] clamped to [0, n - 1].
    """
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    index = math.ceil(q * n) - 1
    index = min(max(index, 0), n - 1)
    return sorted_samples[index]


def _latency_report(samples: list[float]) -> dict | None:
    """Latency summary in milliseconds, or None when there are no samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    count = len(ordered)
    return {
        "count": count,
        "avg": sum(ordered) / count * 1000.0,
        "p50": _percentile(ordered, 0.50) * 1000.0,
        "p95": _percentile(ordered, 0.95) * 1000.0,
        "max": ordered[-1] * 1000.0,
    }


def _requests_report(
    total: int,
    success: int,
    errors: dict[str, int],
    latencies: list[float],
    bytes_in: int,
    bytes_out: int,
) -> dict:
    """Build the requests block shared by global and per-model reports."""
    return {
        "total": total,
        "success": success,
        "failure": total - success,
        "success_rate": (success / total) if total else None,
        "errors": dict(errors),
        "latency_ms": _latency_report(latencies),
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
    }


class StatsCollector:
    """Collects runtime statistics. All mutation happens synchronously
    on the asyncio loop — no locks needed. Recording methods are pure
    arithmetic on plain attributes and never raise.
    """

    def __init__(self) -> None:
        self._started_at = time.time()
        self._total = 0
        self._success = 0
        self._errors: dict[str, int] = {}
        self._latencies: list[float] = []  # seconds
        self._bytes_in = 0
        self._bytes_out = 0
        self._request_times: deque[float] = deque()  # monotonic timestamps
        self._swap_durations: list[float] = []  # seconds
        self._peak_inflight = 0
        self._models: dict[str, _ModelStats] = {}

    def _model(self, name: str) -> _ModelStats:
        stats = self._models.get(name)
        if stats is None:
            stats = _ModelStats()
            self._models[name] = stats
        return stats

    def record_request(
        self,
        model: str | None,
        *,
        ok: bool,
        kind: str | None = None,
        latency_s: float | None = None,
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        """Record a completed proxied request."""
        self._total += 1
        if ok:
            self._success += 1
        elif kind is not None:
            self._errors[kind] = self._errors.get(kind, 0) + 1
        if latency_s is not None:
            self._latencies.append(latency_s)
        self._bytes_in += bytes_in
        self._bytes_out += bytes_out
        self._request_times.append(time.monotonic())
        if model is not None:
            ms = self._model(model)
            ms.total += 1
            if ok:
                ms.success += 1
            elif kind is not None:
                ms.errors[kind] = ms.errors.get(kind, 0) + 1
            if latency_s is not None:
                ms.latencies.append(latency_s)
            ms.bytes_in += bytes_in
            ms.bytes_out += bytes_out
        self._prune_request_times()

    def record_rejection(self, kind: str, model: str | None = None) -> None:
        """Record a request rejected before proxying."""
        self._total += 1
        self._errors[kind] = self._errors.get(kind, 0) + 1
        self._request_times.append(time.monotonic())
        if model is not None:
            ms = self._model(model)
            ms.total += 1
            ms.errors[kind] = ms.errors.get(kind, 0) + 1
        self._prune_request_times()

    def record_swap(self, duration_s: float) -> None:
        """Record a completed swap (stop-then-start through healthy)."""
        self._swap_durations.append(duration_s)

    def record_model_start(self, model: str) -> None:
        """Record that a model process was started."""
        self._model(model).starts += 1

    def record_model_stop(self, model: str) -> None:
        """Record that a model process was stopped (not shutdown)."""
        self._model(model).stops += 1

    def observe_inflight(self, n: int) -> None:
        """Update peak concurrency with the current in-flight count."""
        if n > self._peak_inflight:
            self._peak_inflight = n

    def _prune_request_times(self) -> None:
        now_monotonic = time.monotonic()
        times = self._request_times
        while times and now_monotonic - times[0] > REQUEST_RETENTION_S:
            times.popleft()

    def snapshot(
        self,
        current_model: str | None = None,
        inflight: int = 0,
    ) -> dict:
        """Build the JSON-contract report of all collected statistics."""
        self._prune_request_times()
        now_monotonic = time.monotonic()
        uptime = max(time.time() - self._started_at, 0.0)
        if self._swap_durations:
            ordered = sorted(self._swap_durations)
            count = len(ordered)
            swaps = {
                "count": count,
                "avg_ms": sum(ordered) / count * 1000.0,
                "p95_ms": _percentile(ordered, 0.95) * 1000.0,
                "max_ms": ordered[-1] * 1000.0,
            }
        else:
            swaps = {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
        models = {
            name: {
                "starts": ms.starts,
                "stops": ms.stops,
                "requests": _requests_report(
                    ms.total,
                    ms.success,
                    ms.errors,
                    ms.latencies,
                    ms.bytes_in,
                    ms.bytes_out,
                ),
            }
            for name, ms in self._models.items()
        }
        return {
            "uptime_seconds": uptime,
            "current_model": current_model,
            "concurrency": {
                "active": inflight,
                "peak": self._peak_inflight,
            },
            "requests": _requests_report(
                self._total,
                self._success,
                self._errors,
                self._latencies,
                self._bytes_in,
                self._bytes_out,
            ),
            "throughput": {
                "last_60s": sum(
                    1 for t in self._request_times
                    if now_monotonic - t <= WINDOW_SHORT_S
                ),
                "last_300s": sum(
                    1 for t in self._request_times
                    if now_monotonic - t <= WINDOW_LONG_S
                ),
                "avg_rpm": (self._total / (uptime / 60.0)) if uptime > 0 else 0.0,
            },
            "swaps": swaps,
            "models": models,
        }


STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Switcher — Statistics</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: system-ui, sans-serif;
    margin: 0 auto; padding: 1rem; max-width: 60rem;
    background: light-dark(#fafafa, #1b1b1b); color: light-dark(#222, #eee);
  }
  h1 { font-size: 1.4rem; margin: 0; }
  header p { margin: 0.2rem 0 1rem; color: #888; }
  .cards { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1.25rem; }
  .card {
    flex: 1 1 8rem; border: 1px solid #8884; border-radius: 0.5rem;
    padding: 0.75rem 1rem; min-width: 8rem;
  }
  .card .value { font-size: 1.5rem; font-weight: 600; }
  .card .label { font-size: 0.8rem; color: #888; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { border: 1px solid #8884; padding: 0.35rem 0.6rem; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  caption { caption-side: top; text-align: left; font-weight: 600; padding: 0.3rem 0; }
</style>
</head>
<body>
<header>
  <h1>Model Switcher — Statistics</h1>
  <p>Current model: <span id="current-model">–</span> ·
     Uptime: <span id="uptime">–</span></p>
</header>
<div class="cards">
  <div class="card"><div class="value" id="total">–</div><div class="label">Total requests</div></div>
  <div class="card"><div class="value" id="success-rate">–</div><div class="label">Success rate</div></div>
  <div class="card"><div class="value" id="concurrency">–</div><div class="label">Active / peak requests</div></div>
  <div class="card"><div class="value" id="swaps">–</div><div class="label">Swaps</div></div>
  <div class="card"><div class="value" id="rpm">–</div><div class="label">Requests/min (60s)</div></div>
</div>
<noscript><p>JavaScript is disabled — raw metrics are available at
<a href="/stats/json">/stats/json</a>.</p></noscript>
<table>
  <caption>Error breakdown</caption>
  <thead><tr><th>Kind</th><th>Count</th></tr></thead>
  <tbody id="errors"><tr><td colspan="2">–</td></tr></tbody>
</table>
<table>
  <caption>Per-model statistics</caption>
  <thead><tr>
    <th>Model</th><th>Starts</th><th>Stops</th><th>Requests</th>
    <th>Success</th><th>Failure</th><th>Avg ms</th><th>P50 ms</th>
    <th>P95 ms</th><th>Max ms</th><th>Bytes in</th><th>Bytes out</th>
  </tr></thead>
  <tbody id="models"><tr><td colspan="12">–</td></tr></tbody>
</table>
<script>
"use strict";

const DASH = "–";

function fmtInt(v) {
  return (typeof v === "number" && isFinite(v)) ? String(v) : DASH;
}

function fmtRate(v) {
  return (typeof v === "number" && isFinite(v))
    ? (v * 100).toFixed(1) + "%" : DASH;
}

function fmtMs(v) {
  return (typeof v === "number" && isFinite(v)) ? v.toFixed(1) : DASH;
}

function fmtBytes(v) {
  if (!(typeof v === "number" && isFinite(v))) return DASH;
  const units = ["B", "KiB", "MiB", "GiB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(i ? 1 : 0) + " " + units[i];
}

function fmtUptime(s) {
  if (!(typeof s === "number" && isFinite(s))) return DASH;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return (h ? h + "h " : "") + (m ? m + "m " : "") + sec + "s";
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function fillRowless(tbodyId, cols) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.textContent = "";
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = cols;
  td.textContent = DASH;
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function renderErrors(errors) {
  const tbody = document.getElementById("errors");
  if (!tbody) return;
  const entries = errors && typeof errors === "object"
    ? Object.entries(errors) : [];
  tbody.textContent = "";
  if (!entries.length) { fillRowless("errors", 2); return; }
  for (const [kind, count] of entries) {
    const tr = document.createElement("tr");
    const tdKind = document.createElement("td");
    const tdCount = document.createElement("td");
    tdKind.textContent = String(kind);
    tdCount.textContent = fmtInt(count);
    tr.appendChild(tdKind);
    tr.appendChild(tdCount);
    tbody.appendChild(tr);
  }
}

function renderModels(models) {
  const tbody = document.getElementById("models");
  if (!tbody) return;
  const entries = models && typeof models === "object"
    ? Object.entries(models) : [];
  tbody.textContent = "";
  if (!entries.length) { fillRowless("models", 12); return; }
  for (const [name, m] of entries) {
    const r = (m && m.requests) || {};
    const lat = r.latency_ms || {};
    const cells = [
      String(name),
      fmtInt(m && m.starts),
      fmtInt(m && m.stops),
      fmtInt(r.total),
      fmtInt(r.success),
      fmtInt(r.failure),
      fmtMs(lat.avg), fmtMs(lat.p50), fmtMs(lat.p95), fmtMs(lat.max),
      fmtBytes(r.bytes_in), fmtBytes(r.bytes_out),
    ];
    const tr = document.createElement("tr");
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function render(data) {
  if (!data || typeof data !== "object") return;
  setText("current-model", data.current_model == null ? DASH : String(data.current_model));
  setText("uptime", fmtUptime(data.uptime_seconds));
  const req = data.requests || {};
  setText("total", fmtInt(req.total));
  setText("success-rate", fmtRate(req.success_rate));
  const conc = data.concurrency || {};
  setText("concurrency",
    fmtInt(conc.active) + " / " + fmtInt(conc.peak));
  const swaps = data.swaps || {};
  setText("swaps", fmtInt(swaps.count));
  const thr = data.throughput || {};
  setText("rpm", fmtInt(thr.last_60s));
  renderErrors(req.errors);
  renderModels(data.models);
}

function refresh() {
  fetch("/stats/json").then(function (resp) {
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }).then(render).catch(function () { /* keep last render */ });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
