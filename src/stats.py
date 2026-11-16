"""In-memory runtime statistics collection for the switcher.

StatsCollector lives here. Record types (stats_records), in-flight
tracking and aggregate frontiers (stats_frontier), the live rate
(stats_live), and report blocks (stats_report) live in sibling modules.
This module stays the import surface: it re-exports Outcome, STATS_HTML,
_percentile, and the other names that moved out of it.
"""

from __future__ import annotations

import math
import time
from collections import deque

from src.decode import DecodeDecision, ServerDecode, select_decode
from src.prefill import (
    PrefillDecision,
    ServerPrefill,
    effective_cached,
    select_prefill,
)
from src.stats_frontier import _FrontierRecorder
from src.stats_live import (  # LIVE_BUSY_FLOOR_S: re-export
    LIVE_BUSY_FLOOR_S,
    _live_reports,
)
from src.stats_page import STATS_HTML  # re-export
from src.stats_records import (
    Outcome,  # re-export
    _ActiveRequest,  # re-export
    _apply_usage,
    _LoadStats,
    _ModelStats,
)
from src.stats_report import (
    _global_aggregates,
    _model_report,
    _swaps_report,
    _usage_report,
)
from src.stats_support import (
    REQUEST_RETENTION_S,
    WINDOW_LONG_S,
    WINDOW_SHORT_S,
    _is_count,
    _percentile,  # re-export
    _prune_engine_samples,
    _prune_gen_chunk_events,
    _prune_live_decode_intervals,
    _prune_token_events,
)


class StatsCollector(_FrontierRecorder):
    """Collects runtime statistics. All mutation happens synchronously
    on the asyncio loop — no locks needed. Recording methods are pure
    arithmetic on plain attributes and never raise.
    """

    def __init__(self) -> None:
        self._started_at = time.time()
        # Monotonic start for the utilized-time share (immune to clock jumps).
        self._started_monotonic = time.monotonic()
        # Utilized time: union of periods with >= 1 request in flight.
        self._busy_total = 0.0
        self._busy_since: float | None = None
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
        self.decode_server_tokens = 0
        self.decode_skipped_buffered = 0
        self.prefill_tokens = 0
        self.prefill_time = 0.0
        self.prefill_server_tokens = 0
        self.prompt_skipped_unknown_cache = 0
        self.paired_prefill_time = 0.0
        self.paired_decode_time = 0.0
        self.paired_requests = 0
        self.token_events: deque[tuple[float, int]] = deque()
        self._swap_durations: list[float] = []  # seconds
        self._loads = _LoadStats()
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
        server_prefill: ServerPrefill | None = None,
        server_decode: ServerDecode | None = None,
        gen_chunks: int | None = None,
    ) -> None:
        """Record a completed proxied request.

        Token counters are recorded whenever usage was extractable,
        regardless of response status; timing stats (ttft_s/decode_s)
        are only passed for successful streaming requests. The prompt-speed
        sample of a successful request comes from the server's own prefill
        timing when present, else from ttft_s (see src/prefill.py); the
        generation-speed sample likewise from server decode timing, else
        from decode_s when the stream had enough chunks (src/decode.py).
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
        cached_tokens = effective_cached(cached_tokens, server_prefill)
        decision = self._decide_prefill(
            model, prompt_tokens, cached_tokens, server_prefill, ttft_s,
            ok=ok,
        )
        decode = (
            select_decode(
                completion_tokens, server_decode, decode_s, gen_chunks,
            )
            if ok else DecodeDecision()
        )
        _apply_usage(
            self, now_monotonic, prompt_tokens, completion_tokens,
            cached_tokens, ttft_s, decode, decision,
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
            _apply_usage(
                ms, now_monotonic, prompt_tokens, completion_tokens,
                cached_tokens, ttft_s, decode, decision,
            )
        self._prune_request_times()

    def _decide_prefill(
        self,
        model: str | None,
        prompt_tokens: int | None,
        cached_tokens: int | None,
        server_prefill: ServerPrefill | None,
        proxy_ttft_s: float | None,
        *,
        ok: bool,
    ) -> PrefillDecision:
        """Select one request's prefill sample and learn cache reporting.

        cached_tokens must already be resolved with effective_cached. A
        model that reports a cached count once is remembered, so later
        missing counts mean 0 cached instead of an unknown.
        """
        stats = self._model(model) if model is not None else None
        if stats is not None and cached_tokens is not None:
            stats.cache_reported = True
        if not ok:
            return PrefillDecision()
        return select_prefill(
            prompt_tokens, cached_tokens, server_prefill, proxy_ttft_s,
            cache_learned=stats is not None and stats.cache_reported,
        )

    def record_gen_chunks(self, model: str, n: int) -> None:
        """Record n streamed output chunks for the live rate estimate."""
        if not _is_count(n) or n < 1:
            return
        now_monotonic = time.monotonic()
        events = self._model(model).gen_chunk_events
        events.append((now_monotonic, n))
        _prune_gen_chunk_events(events, now_monotonic)

    def record_engine_tokens(self, model: str, total: object) -> None:
        """Record the engine's generated-token counter for the live rate."""
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return
        if not math.isfinite(total) or total < 0:
            return
        now_monotonic = time.monotonic()
        samples = self._model(model).engine_samples
        samples.append((now_monotonic, float(total)))
        _prune_engine_samples(samples, now_monotonic)

    def clear_engine_tokens(self, model: str) -> None:
        """Forget engine counter samples (a new model process was loaded)."""
        stats = self._models.get(model)
        if stats is not None:
            stats.engine_samples.clear()

    def has_active_decode(self, model: str) -> bool:
        """True while the model has a decode past its first token."""
        stats = self._models.get(model)
        return stats is not None and bool(stats.active_decodes)

    def record_gen_calibration(
        self, model: str, completion_tokens: object, chunks: object,
    ) -> None:
        """Record one finished successful stream's exact tokens and chunks.

        Samples need completion_tokens >= 1 and chunks >= 1; invalid input
        is ignored without raising. Only the last 50 samples are kept.
        """
        if not (_is_count(completion_tokens) and _is_count(chunks)):
            return
        if completion_tokens < 1 or chunks < 1:
            return
        self._model(model).gen_calibration.append((completion_tokens, chunks))

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

    def record_load(
        self,
        model: str,
        load_s: float,
        *,
        stop_s: float | None = None,
        swap: bool = False,
    ) -> None:
        """Record a successful load: process start until healthy.

        stop_s is the time spent stopping the previous model for this load
        (hook + kill), kept apart from load_s; swap marks such loads.
        """
        self._loads.record(load_s, stop_s, swap)
        self._model(model).loads.record(load_s, stop_s, swap)

    def record_load_outcome(self, model: str, bucket: str, kind: str) -> None:
        """Count a started load that did not become healthy.

        bucket is "failed" or "abandoned"; kind is the outcome name.
        """
        self._loads.record_outcome(bucket, kind)
        self._model(model).loads.record_outcome(bucket, kind)

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

    def observe_busy(self, n: int) -> None:
        """Track utilized time from the current in-flight request count.

        A busy period starts on the 0 -> 1 edge and is accumulated on the
        1 -> 0 edge, so overlapping requests count once.
        """
        if n > 0:
            if self._busy_since is None:
                self._busy_since = time.monotonic()
        elif self._busy_since is not None:
            self._busy_total += max(time.monotonic() - self._busy_since, 0.0)
            self._busy_since = None

    def _utilization_report(self, now_monotonic: float) -> dict:
        busy = self._busy_total
        if self._busy_since is not None:
            busy += max(now_monotonic - self._busy_since, 0.0)
        uptime = max(now_monotonic - self._started_monotonic, 0.0)
        share = min(max(busy / uptime, 0.0), 1.0) if uptime > 0 else None
        return {"busy_seconds": busy, "busy_share": share}

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
            _prune_gen_chunk_events(ms.gen_chunk_events, now_monotonic)
            _prune_live_decode_intervals(
                ms.live_decode_intervals, now_monotonic,
            )
            _prune_engine_samples(ms.engine_samples, now_monotonic)
        now_monotonic = time.monotonic()
        live, global_live = _live_reports(
            self._models, now_monotonic, current_model,
        )
        uptime = max(time.time() - self._started_at, 0.0)
        swaps = _swaps_report(self._swap_durations)
        models = {
            name: _model_report(ms, now_monotonic, live[name])
            for name, ms in self._models.items()
        }
        # Global busy-time averages sum the per-model accumulators.
        global_decode, global_prefill = _global_aggregates(self._models)
        return {
            "uptime_seconds": uptime,
            "current_model": current_model,
            "concurrency": {
                "active": inflight,
                "peak": self._peak_inflight,
            },
            "requests": _usage_report(
                self,
                self._total,
                self._success,
                self._errors,
                self._latencies,
                self._bytes_in,
                self._bytes_out,
                now_monotonic=now_monotonic,
                decode_aggregate=global_decode,
                prefill_aggregate=global_prefill,
                # Global: current model's engine rate, else Σ calibrated
                # tokens / union busy time (all models).
                live=global_live,
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
                "avg_rpm": (
                    (self._total / (uptime / 60.0)) if uptime > 0 else 0.0
                ),
            },
            "swaps": swaps,
            "loads": self._loads.report(),
            "models": models,
            "utilization": self._utilization_report(now_monotonic),
        }
