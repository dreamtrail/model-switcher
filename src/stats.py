"""In-memory runtime statistics collection for the switcher."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from src.decode import (
    DecodeDecision,
    ServerDecode,
    decode_interval,
    select_decode,
)
from src.prefill import (
    PrefillDecision,
    ServerPrefill,
    effective_cached,
    prefill_interval,
    select_prefill,
)

# Trailing windows for throughput reporting.
WINDOW_SHORT_S = 60
WINDOW_LONG_S = 300
# Request timestamps older than this are pruned from the deque.
REQUEST_RETENTION_S = 3600
# Live-rate calibration: finished streaming requests kept per model.
GEN_CALIBRATION_SAMPLES = 50
# Live generation-rate window (seconds); exact-token windows stay above.
LIVE_WINDOW_S = 15
# Floor for the live-rate busy-time denominator (seconds).
LIVE_BUSY_FLOOR_S = 1.0


@dataclass
class Outcome:
    """Mutable stream outcome reported back by the proxy functions.

    proxy.py writes to this object; it never touches StatsCollector.
    """

    stream_error: bool = False
    client_disconnect: bool = False  # client went away mid-stream
    bytes_out: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    first_token_at: float | None = None  # monotonic ts of first content event
    first_token_observer: Callable[[float], None] | None = field(
        default=None, repr=False,
    )
    # Streaming only: called with the number of output chunks in each event.
    gen_chunk_observer: Callable[[int], None] | None = field(
        default=None, repr=False,
    )
    # Streaming only: total output chunks seen (live-rate calibration).
    gen_chunks: int = 0
    # Server-reported prefill timing (timings / usage.prompt_time / metrics).
    server_prefill: ServerPrefill = field(default_factory=ServerPrefill)
    # Server-reported decode timing (timings / completion_time / metrics).
    server_decode: ServerDecode = field(default_factory=ServerDecode)


@dataclass(frozen=True)
class _DecodeInterval:
    """One completed, aggregate-eligible decode interval."""

    started_at: float
    ended_at: float
    completion_tokens: int


@dataclass(frozen=True)
class _PrefillInterval:
    """One completed, aggregate-eligible prefill (start → first token)."""

    started_at: float
    ended_at: float
    prefill_tokens: int


@dataclass
class _ActiveRequest:
    """An in-flight proxied request whose prefill may still be unpublished."""

    started_at: float
    first_token_at: float | None = None


@dataclass
class _AggregateTotals:
    """Busy-time accumulators over published interval groups.

    tokens / busy_time is the average aggregate speed while at least one
    stream was busy; stream_time / busy_time is the average concurrency.
    """

    tokens: int = 0
    busy_time: float = 0.0
    stream_time: float = 0.0

    def add(self, other: _AggregateTotals) -> None:
        self.tokens += other.tokens
        self.busy_time += other.busy_time
        self.stream_time += other.stream_time

    def report(self) -> dict | None:
        """JSON block ``{"tps", "concurrency"}``, or None before any group."""
        if self.busy_time <= 0 or self.tokens <= 0:
            return None
        return {
            "tps": self.tokens / self.busy_time,
            "concurrency": self.stream_time / self.busy_time,
        }


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
    decode_tokens: int = 0  # Σ decode-sample tokens (src/decode.py)
    decode_time: float = 0.0  # Σ decode seconds
    decode_server_tokens: int = 0  # part of decode_tokens timed by server
    decode_skipped_buffered: int = 0  # excluded: buffered stream, no timing
    prefill_tokens: int = 0  # Σ uncached prompt (prompt − cached) work
    prefill_time: float = 0.0  # Σ prefill seconds (server time, else TTFT)
    prefill_server_tokens: int = 0  # part of prefill_tokens timed by server
    prompt_skipped_unknown_cache: int = 0  # excluded: cached count unknown
    # Learned: this model has reported a cached-token count at least once.
    cache_reported: bool = False
    token_events: deque = field(default_factory=deque)  # (monotonic, tokens)
    gen_chunk_events: deque = field(default_factory=deque)  # (monotonic, units)
    active_decodes: dict[object, float] = field(default_factory=dict)
    # Live rate: recently ended decodes (started_at, ended_at), any outcome.
    live_decode_intervals: deque = field(default_factory=deque)
    # Live rate: (completion_tokens, chunks) of recent finished streams.
    gen_calibration: deque = field(
        default_factory=lambda: deque(maxlen=GEN_CALIBRATION_SAMPLES),
    )
    decode_frontier: list[_DecodeInterval] = field(default_factory=list)
    max_aggregate_tps: float | None = None
    max_aggregate_concurrency: int | None = None
    decode_aggregate: _AggregateTotals = field(default_factory=_AggregateTotals)
    active_requests: dict[object, _ActiveRequest] = field(default_factory=dict)
    prefill_frontier: list[_PrefillInterval] = field(default_factory=list)
    prefill_aggregate: _AggregateTotals = field(
        default_factory=_AggregateTotals,
    )


_Interval = _DecodeInterval | _PrefillInterval


def _interval_components(
    intervals: list[_Interval],
) -> list[list[_Interval]]:
    """Partition intervals into positive-overlap connected components."""
    ordered = sorted(
        intervals, key=lambda interval: (interval.started_at, interval.ended_at)
    )
    components: list[list[_Interval]] = []
    component_end: float | None = None
    for interval in ordered:
        if component_end is None or interval.started_at >= component_end:
            components.append([interval])
            component_end = interval.ended_at
        else:
            components[-1].append(interval)
            component_end = max(component_end, interval.ended_at)
    return components


def _component_totals(
    component: list[_Interval], tokens: int,
) -> _AggregateTotals | None:
    """Busy span and summed stream time of one connected component.

    A component has no internal gaps, so its span is its busy time.
    Returns None when the values are not finite and positive.
    """
    span = (
        max(interval.ended_at for interval in component)
        - min(interval.started_at for interval in component)
    )
    stream_time = sum(
        interval.ended_at - interval.started_at for interval in component
    )
    if not (
        math.isfinite(span) and span > 0
        and math.isfinite(stream_time) and stream_time > 0
        and tokens >= 1
    ):
        return None
    return _AggregateTotals(
        tokens=tokens, busy_time=span, stream_time=stream_time,
    )


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _peak_decode_concurrency(intervals: list[_DecodeInterval]) -> int:
    """Return peak overlap for half-open intervals; ends precede starts."""
    events = [
        event
        for interval in intervals
        for event in (
            (interval.started_at, 1),
            (interval.ended_at, -1),
        )
    ]
    active = peak = 0
    for _, change in sorted(events, key=lambda event: (event[0], event[1])):
        active += change
        peak = max(peak, active)
    return peak


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


def _prune_gen_chunk_events(events: deque, now_monotonic: float) -> None:
    """Drop streamed-chunk events older than the live window."""
    while events and now_monotonic - events[0][0] > LIVE_WINDOW_S:
        events.popleft()


def _prune_live_decode_intervals(
    intervals: deque, now_monotonic: float,
) -> None:
    """Drop ended decode intervals that end before the live window."""
    while intervals and now_monotonic - intervals[0][1] > LIVE_WINDOW_S:
        intervals.popleft()


def _busy_seconds(
    intervals: list[tuple[float, float]], now_monotonic: float,
) -> float:
    """Length of the union of intervals clipped to the live window."""
    window_start = now_monotonic - LIVE_WINDOW_S
    clipped = sorted(
        (max(start, window_start), min(end, now_monotonic))
        for start, end in intervals
    )
    busy = 0.0
    cur_start = cur_end = None
    for start, end in clipped:
        if end <= start:
            continue
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                busy += cur_end - cur_start
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        busy += cur_end - cur_start
    return busy


def _tokens_per_chunk(samples: deque) -> float:
    """Calibrated tokens per streamed chunk (1.0 without samples)."""
    tokens = sum(t for t, _ in samples)
    chunks = sum(c for _, c in samples)
    return tokens / chunks if tokens > 0 and chunks > 0 else 1.0


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
    decode_server_tokens: int = 0,
    decode_skipped_buffered: int = 0,
    prefill_tokens: int = 0,
    prefill_time: float = 0.0,
    prefill_server_tokens: int = 0,
    prompt_skipped_unknown_cache: int = 0,
    token_events: deque = (),
    now_monotonic: float = 0.0,
    max_aggregate: tuple[float, int] | None = None,
    include_max_aggregate: bool = False,
    decode_aggregate: _AggregateTotals | None = None,
    prefill_aggregate: _AggregateTotals | None = None,
    live: dict | None = None,
) -> dict:
    """Build the requests block shared by global and per-model reports."""
    generation_tps = {
        "avg": (decode_tokens / decode_time) if decode_time > 0 else None,
        "last_60s": _window_tps(token_events, now_monotonic, WINDOW_SHORT_S),
        "last_300s": _window_tps(token_events, now_monotonic, WINDOW_LONG_S),
        "avg_aggregate": (
            decode_aggregate.report() if decode_aggregate is not None else None
        ),
        # Share of decode tokens timed by the server itself.
        "server_timed_share": (
            decode_server_tokens / decode_tokens
        ) if decode_tokens > 0 else None,
        "skipped_buffered": decode_skipped_buffered,
        # Estimate: calibrated streamed chunks per busy second.
        "live": {
            "tps": 0.0, "window_s": LIVE_WINDOW_S, "estimate": True,
            "busy_s": 0.0, "tokens_per_chunk": None,
            **(live or {}),
        },
    }
    if include_max_aggregate:
        generation_tps["max_aggregate"] = (
            {
                "tps": max_aggregate[0],
                "concurrency": max_aggregate[1],
            }
            if max_aggregate is not None else None
        )
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
            "prefill": prefill_tokens,
            # Token-weighted lifetime ratio; null when no prompt tokens.
            "cache_hit_rate": (
                cached_tokens / prompt_tokens
            ) if prompt_tokens > 0 else None,
        },
        "rates": {
            "prompt_tps": {
                "avg": (prefill_tokens / prefill_time) if prefill_time > 0 else None,
                "avg_aggregate": (
                    prefill_aggregate.report()
                    if prefill_aggregate is not None else None
                ),
                # Share of prefill tokens timed by the server itself.
                "server_timed_share": (
                    prefill_server_tokens / prefill_tokens
                ) if prefill_tokens > 0 else None,
                "skipped_unknown_cache": prompt_skipped_unknown_cache,
            },
            "generation_tps": generation_tps,
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
        self.decode_server_tokens = 0
        self.decode_skipped_buffered = 0
        self.prefill_tokens = 0
        self.prefill_time = 0.0
        self.prefill_server_tokens = 0
        self.prompt_skipped_unknown_cache = 0
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
            select_decode(completion_tokens, server_decode, decode_s, gen_chunks)
            if ok else DecodeDecision()
        )
        self._apply_usage(
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
            self._apply_usage(
                ms, now_monotonic, prompt_tokens, completion_tokens,
                cached_tokens, ttft_s, decode, decision,
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
        decode: DecodeDecision,
        decision: PrefillDecision,
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
        gen = decode.sample
        if gen is not None:
            # Generated tokens over decode time (src/decode.py).
            if gen.tokens >= 2:
                stats.tpots.append(gen.seconds / (gen.tokens - 1))
            stats.decode_tokens += gen.tokens
            stats.decode_time += gen.seconds
            if gen.server_timed:
                stats.decode_server_tokens += gen.tokens
        elif decode.skipped_buffered:
            stats.decode_skipped_buffered += 1
        sample = decision.sample
        if sample is not None:
            # Uncached prompt work over its prefill time (src/prefill.py).
            stats.prefill_tokens += sample.tokens
            stats.prefill_time += sample.seconds
            if sample.server_timed:
                stats.prefill_server_tokens += sample.tokens
        elif decision.skipped_unknown_cache:
            stats.prompt_skipped_unknown_cache += 1
        events = stats.token_events
        _prune_token_events(events, now_monotonic)

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

    def record_request_start(
        self, model: str, request_id: object, started_at: float,
    ) -> None:
        """Record an in-flight request whose prefill starts at started_at.

        Must be paired with record_prefill_end for the same request_id
        (the swapper calls it from a ``finally`` block).
        """
        active = self._model(model).active_requests
        if request_id not in active:
            active[request_id] = _ActiveRequest(started_at=started_at)

    def record_decode_start(
        self, model: str, request_id: object, started_at: float,
    ) -> None:
        """Record one request's first-token boundary at most once."""
        stats = self._model(model)
        active = stats.active_decodes
        if request_id not in active:
            active[request_id] = started_at
        request = stats.active_requests.get(request_id)
        if request is not None and request.first_token_at is None:
            request.first_token_at = started_at

    def record_prefill_end(
        self,
        model: str,
        request_id: object,
        *,
        first_token_at: float | None,
        ok: bool,
        prompt_tokens: int | None,
        cached_tokens: int | None,
        server_prefill: ServerPrefill | None = None,
        ended_at: float | None = None,
    ) -> None:
        """Close an in-flight request and publish closed prefill groups.

        Eligibility matches rates.prompt_tps: a successful request with a
        prefill sample (server timing, or a first token) and at least one
        uncached prompt token. Server-timed intervals end at the first
        token (streaming) or start at the request start (non-streaming,
        capped at ended_at).
        """
        stats = self._models.get(model)
        if stats is None:
            return
        request = stats.active_requests.pop(request_id, None)
        if request is None:
            return
        proxy_ttft = (
            first_token_at - request.started_at
            if first_token_at is not None else None
        )
        decision = self._decide_prefill(
            model, prompt_tokens,
            effective_cached(cached_tokens, server_prefill),
            server_prefill, proxy_ttft, ok=ok,
        )
        span = (
            prefill_interval(
                decision.sample, request.started_at, first_token_at, ended_at,
            )
            if decision.sample is not None else None
        )
        if span is not None:
            stats.prefill_frontier.append(_PrefillInterval(
                started_at=span[0],
                ended_at=span[1],
                prefill_tokens=decision.sample.tokens,
            ))
        self._finalize_prefill_frontier(stats)
        # The request no longer blocks decode groups either.
        self._finalize_decode_frontier(stats)

    @staticmethod
    def _finalize_prefill_frontier(stats: _ModelStats) -> None:
        """Publish closed prefill groups no in-flight request can overlap.

        An in-flight request can still add an interval [start, first token)
        overlapping a group [g_start, g_end) only if it started before g_end
        and its first token is unknown or later than g_start.
        """
        retained: list[_PrefillInterval] = []
        active = tuple(stats.active_requests.values())
        for component in _interval_components(stats.prefill_frontier):
            group_start = min(interval.started_at for interval in component)
            group_end = max(interval.ended_at for interval in component)
            if any(
                request.started_at < group_end
                and (
                    request.first_token_at is None
                    or request.first_token_at > group_start
                )
                for request in active
            ):
                retained.extend(component)
                continue
            totals = _component_totals(
                component,
                sum(interval.prefill_tokens for interval in component),
            )
            if totals is not None:
                stats.prefill_aggregate.add(totals)
        stats.prefill_frontier = retained

    def record_decode_end(
        self,
        model: str,
        request_id: object,
        *,
        ended_at: float,
        ok: bool,
        completion_tokens: int | None,
        server_decode: ServerDecode | None = None,
        gen_chunks: int | None = None,
    ) -> None:
        """Close a request's decode and finalize newly closed cohorts.

        Handles streams (first token recorded) and in-flight requests from
        record_request_start (non-streaming too). The aggregate interval
        comes from the same decode sample as rates.generation_tps.avg
        (src/decode.py): proxy-timed [first token, end], server-timed
        [max(start, end - server seconds), end].
        """
        stats = self._models.get(model)
        if stats is None:
            return
        started_at = stats.active_decodes.pop(request_id, None)
        request = stats.active_requests.get(request_id)
        if started_at is None and request is None:
            return
        # Live-rate busy time counts every ended stream decode, any outcome.
        if (
            started_at is not None
            and isinstance(ended_at, (int, float))
            and math.isfinite(started_at)
            and math.isfinite(ended_at)
            and ended_at > started_at
        ):
            stats.live_decode_intervals.append((started_at, ended_at))
            _prune_live_decode_intervals(
                stats.live_decode_intervals, time.monotonic(),
            )
        sample = None
        if ok:
            proxy_decode_s = None
            if (
                started_at is not None
                and isinstance(ended_at, (int, float))
            ):
                proxy_decode_s = ended_at - started_at
            sample = select_decode(
                completion_tokens, server_decode, proxy_decode_s, gen_chunks,
            ).sample
        span = (
            decode_interval(
                sample,
                request.started_at if request is not None else None,
                started_at,
                ended_at,
            )
            if sample is not None else None
        )
        if span is not None:
            stats.decode_frontier.append(_DecodeInterval(
                started_at=span[0],
                ended_at=span[1],
                completion_tokens=sample.tokens,
            ))
        self._finalize_decode_frontier(stats, ending=request_id)

    @staticmethod
    def _finalize_decode_frontier(
        stats: _ModelStats, ending: object = None,
    ) -> None:
        """Publish ended components no in-flight request can still bridge.

        A server-timed interval may start anywhere after its request's
        start, so every in-flight request (except the one ending now) that
        started before a component's end blocks it, as does any active
        stream decode.
        """
        retained: list[_DecodeInterval] = []
        active_starts = (
            *stats.active_decodes.values(),
            *(
                request.started_at
                for request_id, request in stats.active_requests.items()
                if request_id is not ending
            ),
        )
        for component in _interval_components(stats.decode_frontier):
            component_end = max(interval.ended_at for interval in component)
            if any(started_at < component_end for started_at in active_starts):
                retained.extend(component)
                continue

            cohort_start = min(
                interval.started_at for interval in component
            )
            cohort_end = component_end
            tokens = sum(
                interval.completion_tokens for interval in component
            )
            try:
                tps = tokens / (cohort_end - cohort_start)
            except (OverflowError, ZeroDivisionError):
                continue
            if not math.isfinite(tps) or tps <= 0:
                continue
            totals = _component_totals(component, tokens)
            if totals is not None:
                stats.decode_aggregate.add(totals)
            concurrency = _peak_decode_concurrency(component)
            if (
                stats.max_aggregate_tps is None
                or tps > stats.max_aggregate_tps
            ):
                stats.max_aggregate_tps = tps
                stats.max_aggregate_concurrency = concurrency
        stats.decode_frontier = retained

    def _prune_request_times(self) -> None:
        now_monotonic = time.monotonic()
        times = self._request_times
        while times and now_monotonic - times[0] > REQUEST_RETENTION_S:
            times.popleft()

    def _live_reports(
        self, now_monotonic: float,
    ) -> tuple[dict[str, dict], dict]:
        """Per-model and global live generation-rate estimates.

        Calibrated tokens (chunks in the 15 s window × tokens per chunk)
        divided by busy time: the union of ended and active decode
        intervals clipped to the window, floored at 1 s. The rate is 0
        while no decode is active.
        """
        per_model: dict[str, dict] = {}
        all_intervals: list[tuple[float, float]] = []
        total_tokens = 0.0
        any_active = False
        for name, ms in self._models.items():
            intervals = list(ms.live_decode_intervals)
            intervals.extend(
                (started_at, now_monotonic)
                for started_at in ms.active_decodes.values()
                if math.isfinite(started_at)
            )
            all_intervals.extend(intervals)
            busy = _busy_seconds(intervals, now_monotonic)
            ratio = _tokens_per_chunk(ms.gen_calibration)
            chunks = sum(
                n for t, n in ms.gen_chunk_events
                if now_monotonic - t <= LIVE_WINDOW_S
            )
            tokens = chunks * ratio
            total_tokens += tokens
            active = bool(ms.active_decodes)
            any_active = any_active or active
            per_model[name] = {
                "tps": (
                    tokens / max(busy, LIVE_BUSY_FLOOR_S) if active else 0.0
                ),
                "busy_s": busy,
                "tokens_per_chunk": ratio,
            }
        busy = _busy_seconds(all_intervals, now_monotonic)
        global_live = {
            "tps": (
                total_tokens / max(busy, LIVE_BUSY_FLOOR_S)
                if any_active else 0.0
            ),
            "busy_s": busy,
            "tokens_per_chunk": None,
        }
        return per_model, global_live

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
        now_monotonic = time.monotonic()
        live, global_live = self._live_reports(now_monotonic)
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
                    decode_server_tokens=ms.decode_server_tokens,
                    decode_skipped_buffered=ms.decode_skipped_buffered,
                    prefill_tokens=ms.prefill_tokens,
                    prefill_time=ms.prefill_time,
                    prefill_server_tokens=ms.prefill_server_tokens,
                    prompt_skipped_unknown_cache=(
                        ms.prompt_skipped_unknown_cache
                    ),
                    token_events=ms.token_events,
                    now_monotonic=now_monotonic,
                    max_aggregate=(
                        ms.max_aggregate_tps,
                        ms.max_aggregate_concurrency,
                    ) if (
                        ms.max_aggregate_tps is not None
                        and ms.max_aggregate_concurrency is not None
                    ) else None,
                    include_max_aggregate=True,
                    decode_aggregate=ms.decode_aggregate,
                    prefill_aggregate=ms.prefill_aggregate,
                    live=live[name],
                ),
            }
            for name, ms in self._models.items()
        }
        # Global busy-time averages sum the per-model accumulators.
        global_decode = _AggregateTotals()
        global_prefill = _AggregateTotals()
        for ms in self._models.values():
            global_decode.add(ms.decode_aggregate)
            global_prefill.add(ms.prefill_aggregate)
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
                decode_server_tokens=self.decode_server_tokens,
                decode_skipped_buffered=self.decode_skipped_buffered,
                prefill_tokens=self.prefill_tokens,
                prefill_time=self.prefill_time,
                prefill_server_tokens=self.prefill_server_tokens,
                prompt_skipped_unknown_cache=(
                    self.prompt_skipped_unknown_cache
                ),
                token_events=self.token_events,
                now_monotonic=now_monotonic,
                decode_aggregate=global_decode,
                prefill_aggregate=global_prefill,
                # Global: Σ calibrated tokens / union busy time (all models).
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
  :root {
    color-scheme: light;
    --plane: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --hairline: #e1e0d9;
    --baseline: #c3c2b7;
    --ring: rgba(11, 11, 11, 0.10);
    --wash: rgba(11, 11, 11, 0.03);
    --accent: #2a78d6;
    --track: #b7d3f6;
    --good: #0ca30c;
    --serious: #ec835a;
    --critical: #d03b3b;
    --good-tint: rgba(12, 163, 12, 0.10);
    --critical-tint: rgba(208, 59, 59, 0.08);
    --shadow: 0 1px 2px rgba(11, 11, 11, 0.04), 0 2px 6px rgba(11, 11, 11, 0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --hairline: #2c2c2a;
      --baseline: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --wash: rgba(255, 255, 255, 0.04);
      --accent: #3987e5;
      --track: #184f95;
      --good-tint: rgba(12, 163, 12, 0.18);
      --critical-tint: rgba(208, 59, 59, 0.16);
      --shadow: none;
    }
  }
  *, *::before, *::after { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html { background: var(--plane); }
  body {
    margin: 0; background: var(--plane); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 15px; line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  main { max-width: 80rem; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; }
  .top {
    display: flex; flex-wrap: wrap; align-items: flex-end;
    justify-content: space-between; gap: 0.5rem 1.5rem; margin-bottom: 1.25rem;
  }
  h1 { margin: 0; font-size: 1.375rem; font-weight: 650; letter-spacing: -0.01em; }
  .subtitle { margin: 0.1rem 0 0; color: var(--ink-2); font-size: 0.9rem; }
  .live {
    display: inline-flex; align-items: center; gap: 0.45rem;
    color: var(--ink-2); font-size: 0.8125rem; white-space: nowrap;
  }
  .dot {
    display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%;
    flex: none; background: var(--muted);
  }
  .dot.good { background: var(--good); }
  .dot.serious { background: var(--serious); }
  .dot.critical { background: var(--critical); }
  .live .dot.good { box-shadow: 0 0 0 3px var(--good-tint); }
  .card {
    background: var(--surface); border: 1px solid var(--ring);
    border-radius: 12px; box-shadow: var(--shadow); min-width: 0;
  }
  .kpis {
    display: grid; gap: 0.75rem; margin-bottom: 1rem;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  /* Seven tiles; the loaded-model tile spans two columns so 2/4/8-column
     grids fill every row without an orphan tile. */
  .kpis .tile.wide { grid-column: span 2; }
  @media (min-width: 40rem) { .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
  @media (min-width: 72rem) { .kpis { grid-template-columns: repeat(8, minmax(0, 1fr)); } }
  .tile { padding: 0.85rem 1rem 0.8rem; }
  .tile .label { color: var(--ink-2); font-size: 0.8rem; }
  .tile .value {
    margin-top: 0.3rem; font-size: 1.6rem; font-weight: 600; line-height: 1.15;
    letter-spacing: -0.015em; font-variant-numeric: proportional-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tile .value.idle { color: var(--muted); }
  .tile .value.name {
    display: -webkit-box; -webkit-box-orient: vertical;
    -webkit-line-clamp: 2; line-clamp: 2;
    min-height: 1.84rem; font-size: 1.15rem; line-height: 1.6;
    letter-spacing: 0; white-space: normal; overflow-wrap: anywhere;
  }
  .tile .value.name.long {
    font-size: 1.05rem; line-height: 1.3; word-break: break-all;
  }
  .tile .unit {
    margin-left: 0.25rem; color: var(--muted); font-size: 0.8rem;
    font-weight: 500; letter-spacing: 0;
  }
  .tile .sub {
    margin-top: 0.3rem; color: var(--muted); font-size: 0.78rem; line-height: 1.35;
  }
  .tile .sub .line {
    display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tile .sub .dot { margin-right: 0.35rem; vertical-align: 0.05em; }
  .tile .sub .serving { color: var(--ink-2); }
  .section { margin-bottom: 1rem; }
  .section h2 {
    margin: 0; padding: 0.9rem 1.25rem 0.6rem;
    font-size: 0.95rem; font-weight: 600;
  }
  .table-wrap { overflow-x: auto; padding-bottom: 0.35rem; }
  table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-variant-numeric: tabular-nums; font-size: 0.875rem;
  }
  th, td { padding: 0.6rem 0.75rem; text-align: right; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; padding-left: 1.25rem; }
  th:last-child, td:last-child { padding-right: 1.25rem; }
  th {
    color: var(--muted); font-size: 0.75rem; font-weight: 500;
    padding-top: 0.35rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--hairline);
  }
  tbody td { border-bottom: 1px solid var(--hairline); }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--wash); }
  tfoot td { border-top: 1px solid var(--baseline); color: var(--ink-2); }
  tfoot td:first-child { color: var(--ink); font-weight: 600; }
  tbody:empty + tfoot td { border-top: none; }
  .model-name {
    display: inline-block; max-width: 12rem; vertical-align: bottom;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-weight: 500;
  }
  tr.loaded .model-name { font-weight: 600; }
  .pill {
    display: inline-flex; align-items: center; gap: 0.3rem; margin-left: 0.5rem;
    padding: 0.05rem 0.5rem 0.05rem 0.4rem; border-radius: 999px;
    background: var(--good-tint); color: var(--ink-2);
    font-size: 0.7rem; font-weight: 500; vertical-align: 0.1em;
  }
  .pill .dot { width: 0.4rem; height: 0.4rem; }
  .muted { color: var(--muted); }
  td .dot.critical { margin-right: 0.4rem; vertical-align: 0.1em; }
  .meter {
    display: inline-block; width: 4rem; height: 6px; margin-right: 0.6rem;
    border-radius: 3px; background: var(--track); overflow: hidden;
    vertical-align: 0.15em;
  }
  .meter .fill { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
  .pct { display: inline-block; min-width: 2.5em; }
  .chips { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0 1.25rem 1.1rem; }
  .chip {
    display: inline-flex; align-items: center; gap: 0.45rem;
    padding: 0.25rem 0.7rem 0.25rem 0.6rem; border-radius: 999px;
    background: var(--critical-tint); font-size: 0.8125rem;
  }
  .chip .count { color: var(--ink-2); font-weight: 600; font-variant-numeric: tabular-nums; }
  footer { margin-top: 1.25rem; color: var(--muted); font-size: 0.78rem; }
  footer a, noscript a, .subtitle a { color: inherit; text-underline-offset: 2px; }
  footer a:hover, .subtitle a:hover { color: var(--accent); }
  @media (max-width: 34rem) {
    main { padding: 1.25rem 0.875rem 2rem; }
    .tile .value { font-size: 1.35rem; }
  }
</style>
</head>
<body>
<main>
<header class="top">
  <div>
    <h1>Model Switcher</h1>
    <p class="subtitle">Statistics · <a href="/control">Control panel</a></p>
  </div>
  <div class="live" role="status">
    <span id="live-dot" class="dot"></span><span id="live-text">Connecting…</span>
  </div>
</header>
<noscript><p>JavaScript is disabled — raw metrics are available at
<a href="/stats/json">/stats/json</a>.</p></noscript>
<section class="kpis" aria-label="Summary">
  <div class="card tile wide">
    <div class="label">Loaded model</div>
    <div id="kpi-model" class="value name idle">–</div>
    <div class="sub"><span id="kpi-model-dot" class="dot good" hidden></span><span id="kpi-model-sub">–</span></div>
  </div>
  <div class="card tile">
    <div class="label">Generation</div>
    <div class="value"><span id="kpi-gen">–</span><span id="kpi-gen-unit" class="unit" hidden>tok/s</span></div>
    <div class="sub">average</div>
  </div>
  <div class="card tile" title="Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)">
    <div class="label">Live gen t/s ≈</div>
    <div id="kpi-live" class="value">–</div>
    <div class="sub">estimate · 15 s</div>
  </div>
  <div class="card tile">
    <div class="label">Requests</div>
    <div id="kpi-reqs" class="value">–</div>
    <div id="kpi-reqs-sub" class="sub">–</div>
  </div>
  <div class="card tile">
    <div class="label">Active now</div>
    <div id="kpi-active" class="value">–</div>
    <div id="kpi-active-sub" class="sub">–</div>
  </div>
  <div class="card tile">
    <div class="label">Swaps</div>
    <div id="kpi-swaps" class="value">–</div>
    <div id="kpi-swaps-sub" class="sub">–</div>
  </div>
  <div class="card tile">
    <div class="label">Uptime</div>
    <div id="kpi-uptime" class="value">–</div>
    <div class="sub">since start</div>
  </div>
</section>
<section class="card section">
  <h2>Models</h2>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>Model</th><th>Reqs</th><th>Errors</th><th title="Generation speed; uses the server's own decode time when available, else first to last streamed chunk">Gen t/s</th>
      <th title="Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)">Live t/s ≈</th>
      <th title="Combined speed while busy @ average concurrent requests">Gen total</th>
      <th title="Prompt processing speed; uses the server's own prefill time when available, else time to first token">Prompt t/s</th>
      <th title="Combined speed while busy @ average concurrent requests">Prompt total</th>
      <th>TTFT p50 / p95</th><th>Cache</th><th>Gen tok</th><th>Prefill tok</th>
    </tr></thead>
    <tbody id="models"></tbody>
    <tfoot id="totals"></tfoot>
  </table>
  </div>
</section>
<section id="errors-card" class="card section" hidden>
  <h2>Errors</h2>
  <div id="errors" class="chips"></div>
</section>
<footer>Raw data: <a href="/stats/json">/stats/json</a></footer>
</main>
<script>
"use strict";

const DASH = "–";

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function obj(v) {
  return (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
}

function fmtInt(v) {
  return isNum(v) ? Math.round(v).toLocaleString("en-US") : DASH;
}

function fmtTps(v) {
  return isNum(v) ? v.toFixed(1) : DASH;
}

function fmtCompact(v) {
  if (!isNum(v)) return DASH;
  const n = Math.round(v);
  const a = Math.abs(n);
  if (a < 10000) return n.toLocaleString("en-US");
  if (a < 999950) return (n / 1e3).toFixed(1) + "K";
  if (a < 999995000) return (n / 1e6).toFixed(2) + "M";
  return (n / 1e9).toFixed(2) + "B";
}

function fmtLive(live) {
  const tps = obj(live).tps;
  return isNum(tps) && tps > 0 ? fmtTps(tps) : DASH;
}

function fmtPct(v) {
  return isNum(v) ? Math.round(v * 100) + "%" : DASH;
}

function fmtAggregate(v, fmtRate) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return DASH;
  if (!(isNum(v.tps) && v.tps > 0)) return DASH;
  if (!(isNum(v.concurrency) && v.concurrency > 0)) return DASH;
  return fmtRate(v.tps) + " @" + v.concurrency.toFixed(1);
}

function fmtTtft(t) {
  t = obj(t);
  if (!isNum(t.p50) && !isNum(t.p95)) return DASH;
  return fmtInt(t.p50) + " / " + fmtInt(t.p95);
}

function fmtUptime(s) {
  if (!isNum(s) || s < 0) return DASH;
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + m + "m";
  if (m) return m + "m " + sec + "s";
  return sec + "s";
}

function fmtSeconds(ms) {
  return isNum(ms) ? (ms / 1000).toFixed(1) + " s" : DASH;
}

function byId(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const el = byId(id);
  if (el) el.textContent = text;
  return el;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setCount(id, v) {
  const node = setText(id, fmtCompact(v));
  if (node) node.title = isNum(v) ? fmtInt(v) : "";
  return node;
}

const LONG_NAME = 12;

function currentName(data) {
  return data.current_model == null ? null : String(data.current_model);
}

function renderTiles(data) {
  const current = currentName(data);
  const req = obj(data.requests);
  const conc = obj(data.concurrency);
  const swaps = obj(data.swaps);
  const thr = obj(data.throughput);

  const model = setText("kpi-model", current == null ? "None" : current);
  if (model) {
    model.className = current == null ? "value name idle" :
      (current.length > LONG_NAME ? "value name long" : "value name");
    model.title = current == null ? "" : current;
  }
  const dot = byId("kpi-model-dot");
  if (dot) dot.hidden = current == null;
  const modelSub = setText("kpi-model-sub", current == null ? "Idle" : "Serving");
  if (modelSub) modelSub.className = current == null ? "" : "serving";

  const genRates = obj(obj(req.rates).generation_tps);
  const gen = fmtTps(genRates.avg);
  setText("kpi-gen", gen);
  const unit = byId("kpi-gen-unit");
  if (unit) unit.hidden = gen === DASH;
  const live = setText("kpi-live", fmtLive(genRates.live));
  if (live) live.className = live.textContent === DASH ? "value idle" : "value";

  setCount("kpi-reqs", req.total);
  const reqSub = byId("kpi-reqs-sub");
  if (reqSub) {
    reqSub.textContent = "";
    reqSub.appendChild(el("span", "line",
      fmtInt(req.failure) + (req.failure === 1 ? " error" : " errors")));
    reqSub.appendChild(el("span", "line", fmtInt(thr.last_60s) + " in last min"));
  }

  setCount("kpi-active", conc.active);
  setText("kpi-active-sub", "peak " + fmtInt(conc.peak));

  setCount("kpi-swaps", swaps.count);
  setText("kpi-swaps-sub", "avg " + fmtSeconds(swaps.avg_ms));

  setText("kpi-uptime", fmtUptime(data.uptime_seconds));
}

function rowCells(name, req) {
  const r = obj(req);
  const rates = obj(r.rates);
  const gt = obj(rates.generation_tps);
  const pt = obj(rates.prompt_tps);
  const tokens = obj(r.tokens);
  return [
    name,
    fmtInt(r.total),
    r.failure,
    fmtTps(gt.avg),
    fmtLive(gt.live),
    fmtAggregate(gt.avg_aggregate, fmtTps),
    fmtInt(pt.avg),
    fmtAggregate(pt.avg_aggregate, fmtInt),
    fmtTtft(r.ttft_ms),
    tokens.cache_hit_rate,
    tokens.completion,
    tokens.prefill,
  ];
}

function nameCell(name, loaded) {
  const td = el("td");
  const label = el("span", "model-name", name);
  label.title = name;
  td.appendChild(label);
  if (loaded) {
    const pill = el("span", "pill");
    pill.appendChild(el("span", "dot good"));
    pill.appendChild(el("span", "", "Loaded"));
    td.appendChild(pill);
  }
  return td;
}

function errorsCell(failure) {
  const td = el("td");
  if (isNum(failure) && failure > 0) {
    td.appendChild(el("span", "dot critical"));
    td.appendChild(el("span", "", fmtInt(failure)));
  } else {
    td.appendChild(el("span", "muted", fmtInt(failure)));
  }
  return td;
}

function ttftCell(text) {
  const td = el("td");
  if (text === DASH) {
    td.appendChild(el("span", "muted", DASH));
  } else {
    td.appendChild(el("span", "", text));
    td.appendChild(el("span", "muted", " ms"));
  }
  return td;
}

function cacheCell(rate) {
  const td = el("td");
  if (!isNum(rate)) {
    td.appendChild(el("span", "muted", DASH));
    return td;
  }
  const meter = el("span", "meter");
  meter.setAttribute("aria-hidden", "true");
  const fill = el("span", "fill");
  fill.style.width = Math.min(100, Math.max(0, rate * 100)).toFixed(1) + "%";
  meter.appendChild(fill);
  td.appendChild(meter);
  td.appendChild(el("span", "pct", fmtPct(rate)));
  return td;
}

function plainCell(text) {
  return el("td", text === DASH ? "muted" : "", text);
}

function countCell(v) {
  const td = plainCell(fmtCompact(v));
  td.title = isNum(v) ? fmtInt(v) : "";
  return td;
}

function buildRow(cells, loaded, className) {
  const tr = el("tr", className);
  tr.appendChild(nameCell(cells[0], loaded));
  tr.appendChild(plainCell(cells[1]));
  tr.appendChild(errorsCell(cells[2]));
  for (const text of cells.slice(3, 8)) tr.appendChild(plainCell(text));
  tr.appendChild(ttftCell(cells[8]));
  tr.appendChild(cacheCell(cells[9]));
  tr.appendChild(countCell(cells[10]));
  tr.appendChild(countCell(cells[11]));
  return tr;
}

function orderedModels(models, current) {
  const total = (m) => {
    const t = obj(obj(m).requests).total;
    return isNum(t) ? t : 0;
  };
  return Object.entries(obj(models)).sort(function (a, b) {
    const aCur = current != null && a[0] === current;
    const bCur = current != null && b[0] === current;
    if (aCur !== bCur) return aCur ? -1 : 1;
    const diff = total(b[1]) - total(a[1]);
    if (diff) return diff;
    return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0);
  });
}

function renderTable(data) {
  const tbody = byId("models");
  const current = currentName(data);
  if (tbody) {
    tbody.textContent = "";
    for (const [name, m] of orderedModels(data.models, current)) {
      const loaded = name === current;
      tbody.appendChild(buildRow(rowCells(String(name), obj(m).requests),
        loaded, loaded ? "loaded" : ""));
    }
  }
  const tfoot = byId("totals");
  if (tfoot) {
    tfoot.textContent = "";
    tfoot.appendChild(buildRow(rowCells("All models", data.requests),
      false, "all"));
  }
}

function renderErrors(errors) {
  const box = byId("errors");
  const card = byId("errors-card");
  const entries = Object.entries(obj(errors))
    .filter(function (e) { return isNum(e[1]) && e[1] > 0; })
    .sort(function (a, b) {
      return (b[1] - a[1]) || (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0));
    });
  if (box) {
    box.textContent = "";
    for (const [kind, count] of entries) {
      const chip = el("span", "chip");
      chip.appendChild(el("span", "dot critical"));
      chip.appendChild(el("span", "kind", String(kind)));
      chip.appendChild(el("span", "count", fmtInt(count)));
      box.appendChild(chip);
    }
  }
  if (card) card.hidden = !entries.length;
}

let lastOkAt = null;
let fetchFailed = false;

function renderLive() {
  const dot = byId("live-dot");
  let text;
  let tone;
  if (fetchFailed) {
    tone = "serious";
    text = "Connection lost · " +
      (lastOkAt == null ? "retrying" : "showing last data");
  } else if (lastOkAt == null) {
    tone = "";
    text = "Connecting…";
  } else {
    tone = "good";
    const secs = Math.max(0, Math.floor((Date.now() - lastOkAt) / 1000));
    text = "Live · updated " + secs + "s ago";
  }
  if (dot) dot.className = tone ? "dot " + tone : "dot";
  setText("live-text", text);
}

function render(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return;
  renderTiles(data);
  renderTable(data);
  renderErrors(obj(data.requests).errors);
}

function refresh() {
  fetch("/stats/json").then(function (resp) {
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }).then(function (data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("unexpected payload");
    }
    render(data);
    lastOkAt = Date.now();
    fetchFailed = false;
    renderLive();
  }).catch(function () {
    /* keep last render */
    fetchFailed = true;
    renderLive();
  });
}

refresh();
setInterval(renderLive, 1000);
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
