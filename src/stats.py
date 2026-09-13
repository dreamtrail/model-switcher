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
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    first_token_at: float | None = None  # monotonic ts of first content event


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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    ttfts: list[float] = field(default_factory=list)  # seconds
    tpots: list[float] = field(default_factory=list)  # seconds
    decode_tokens: int = 0  # Σ completion_tokens over streaming requests
    decode_time: float = 0.0  # Σ decode seconds
    prefill_tokens: int = 0  # Σ prompt_tokens over streaming requests
    prefill_time: float = 0.0  # Σ TTFT seconds
    token_events: deque = field(default_factory=deque)  # (monotonic, tokens)


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


def _prune_token_events(events: deque, now_monotonic: float) -> None:
    """Drop token events older than the retention window (chronological)."""
    while events and now_monotonic - events[0][0] > REQUEST_RETENTION_S:
        events.popleft()


def _window_tps(
    events: deque,
    now_monotonic: float,
    window_s: int,
) -> float:
    """Completion tokens within the trailing window divided by window size."""
    tokens = sum(n for t, n in events if now_monotonic - t <= window_s)
    return tokens / window_s


def _requests_report(
    total: int,
    success: int,
    errors: dict[str, int],
    latencies: list[float],
    bytes_in: int,
    bytes_out: int,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    ttfts: list[float] | None = None,
    tpots: list[float] | None = None,
    decode_tokens: int = 0,
    decode_time: float = 0.0,
    prefill_tokens: int = 0,
    prefill_time: float = 0.0,
    token_events: deque = (),
    now_monotonic: float = 0.0,
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
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "cached": cached_tokens,
        },
        "rates": {
            "prompt_tps": {
                "avg": (prefill_tokens / prefill_time) if prefill_time > 0 else None,
            },
            "generation_tps": {
                "avg": (decode_tokens / decode_time) if decode_time > 0 else None,
                "last_60s": _window_tps(token_events, now_monotonic, WINDOW_SHORT_S),
                "last_300s": _window_tps(token_events, now_monotonic, WINDOW_LONG_S),
            },
        },
        "ttft_ms": _latency_report(ttfts or []),
        "tpot_ms": _latency_report(tpots or []),
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
        # Global mirrors of the per-model token/timing stats (same field
        # names as _ModelStats so _apply_usage works on both).
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.ttfts: list[float] = []  # seconds
        self.tpots: list[float] = []  # seconds
        self.decode_tokens = 0
        self.decode_time = 0.0
        self.prefill_tokens = 0
        self.prefill_time = 0.0
        self.token_events: deque[tuple[float, int]] = deque()
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
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cached_tokens: int | None = None,
        ttft_s: float | None = None,
        decode_s: float | None = None,
    ) -> None:
        """Record a completed proxied request.

        Token counters are recorded whenever usage was extractable,
        regardless of response status; timing stats (ttft_s/decode_s)
        are only passed for successful streaming requests.
        """
        self._total += 1
        if ok:
            self._success += 1
        elif kind is not None:
            self._errors[kind] = self._errors.get(kind, 0) + 1
        if latency_s is not None:
            self._latencies.append(latency_s)
        self._bytes_in += bytes_in
        self._bytes_out += bytes_out
        now_monotonic = time.monotonic()
        self._request_times.append(now_monotonic)
        self._apply_usage(
            self, now_monotonic, prompt_tokens, completion_tokens,
            cached_tokens, ttft_s, decode_s,
        )
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
            self._apply_usage(
                ms, now_monotonic, prompt_tokens, completion_tokens,
                cached_tokens, ttft_s, decode_s,
            )
        self._prune_request_times()

    @staticmethod
    def _apply_usage(
        stats: _ModelStats,
        now_monotonic: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cached_tokens: int | None,
        ttft_s: float | None,
        decode_s: float | None,
    ) -> None:
        """Apply usage/timing contributions to a stats holder.

        Works for both the global mirrors and per-model stats (same field
        names). Pure arithmetic — every derivation is None-guarded and
        never raises.
        """
        if prompt_tokens is not None:
            stats.prompt_tokens += prompt_tokens
        if completion_tokens is not None:
            stats.completion_tokens += completion_tokens
            stats.token_events.append((now_monotonic, completion_tokens))
        if cached_tokens is not None:
            stats.cached_tokens += cached_tokens
        if ttft_s is not None:
            stats.ttfts.append(ttft_s)
        if (decode_s is not None and completion_tokens is not None
                and completion_tokens >= 2):
            stats.tpots.append(decode_s / (completion_tokens - 1))
        if (decode_s is not None and completion_tokens is not None
                and completion_tokens >= 1):
            stats.decode_tokens += completion_tokens
            stats.decode_time += decode_s
        if (ttft_s is not None and prompt_tokens is not None
                and prompt_tokens >= 1):
            stats.prefill_tokens += prompt_tokens
            stats.prefill_time += ttft_s
        events = stats.token_events
        _prune_token_events(events, now_monotonic)

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
        _prune_token_events(self.token_events, now_monotonic)
        for ms in self._models.values():
            _prune_token_events(ms.token_events, now_monotonic)
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
                    prompt_tokens=ms.prompt_tokens,
                    completion_tokens=ms.completion_tokens,
                    cached_tokens=ms.cached_tokens,
                    ttfts=ms.ttfts,
                    tpots=ms.tpots,
                    decode_tokens=ms.decode_tokens,
                    decode_time=ms.decode_time,
                    prefill_tokens=ms.prefill_tokens,
                    prefill_time=ms.prefill_time,
                    token_events=ms.token_events,
                    now_monotonic=now_monotonic,
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
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                cached_tokens=self.cached_tokens,
                ttfts=self.ttfts,
                tpots=self.tpots,
                decode_tokens=self.decode_tokens,
                decode_time=self.decode_time,
                prefill_tokens=self.prefill_tokens,
                prefill_time=self.prefill_time,
                token_events=self.token_events,
                now_monotonic=now_monotonic,
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
  <div class="card"><div class="value" id="gen-tps">–</div><div class="label">Gen tok/s (avg)</div></div>
  <div class="card"><div class="value" id="completion-tokens">–</div><div class="label">Completion tokens</div></div>
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
<table>
  <caption>Token throughput</caption>
  <thead><tr>
    <th>Model</th><th>Prompt tok</th><th>Compl tok</th><th>Cached tok</th>
    <th>Prompt t/s</th><th>Gen t/s</th><th>TTFT p50 ms</th><th>TTFT p95 ms</th>
  </tr></thead>
  <tbody id="token-models"><tr><td colspan="8">–</td></tr></tbody>
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

function fmtTps(v) {
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

function renderTokenModels(models) {
  const tbody = document.getElementById("token-models");
  if (!tbody) return;
  const entries = models && typeof models === "object"
    ? Object.entries(models) : [];
  tbody.textContent = "";
  if (!entries.length) { fillRowless("token-models", 8); return; }
  for (const [name, m] of entries) {
    const r = (m && m.requests) || {};
    const toks = r.tokens || {};
    const rates = r.rates || {};
    const pt = rates.prompt_tps || {};
    const gt = rates.generation_tps || {};
    const ttft = r.ttft_ms || {};
    const cells = [
      String(name),
      fmtInt(toks.prompt), fmtInt(toks.completion), fmtInt(toks.cached),
      fmtTps(pt.avg), fmtTps(gt.avg),
      fmtMs(ttft.p50), fmtMs(ttft.p95),
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
  const rates = req.rates || {};
  setText("gen-tps", fmtTps((rates.generation_tps || {}).avg));
  setText("completion-tokens", fmtInt((req.tokens || {}).completion));
  renderErrors(req.errors);
  renderModels(data.models);
  renderTokenModels(data.models);
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
