"""Interval values, aggregate tallies, and pure statistics calculations."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

# Trailing windows for throughput reporting.
WINDOW_SHORT_S = 60
WINDOW_LONG_S = 300
# Request timestamps older than this are pruned from the deque.
REQUEST_RETENTION_S = 3600
# Live-rate calibration: finished streaming requests kept per model.
GEN_CALIBRATION_SAMPLES = 50
# Live generation-rate window (seconds); exact-token windows stay above.
LIVE_WINDOW_S = 5


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


def _duration_report(samples: list[float]) -> dict:
    """count/avg/p95/max in milliseconds; null stats when there are none."""
    if not samples:
        return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(samples)
    count = len(ordered)
    return {
        "count": count,
        "avg_ms": sum(ordered) / count * 1000.0,
        "p95_ms": _percentile(ordered, 0.95) * 1000.0,
        "max_ms": ordered[-1] * 1000.0,
    }


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


def _prune_engine_samples(samples: deque, now_monotonic: float) -> None:
    """Drop engine counter samples older than the live window."""
    while samples and now_monotonic - samples[0][0] > LIVE_WINDOW_S:
        samples.popleft()


def _busy_seconds(
    intervals: list[tuple[float, float]], now_monotonic: float,
) -> float:
    """Length of the union of intervals clipped to the live window."""
    return _busy_between(
        intervals, now_monotonic - LIVE_WINDOW_S, now_monotonic,
    )


def _busy_between(
    intervals: list[tuple[float, float]], span_start: float, span_end: float,
) -> float:
    """Length of the union of intervals clipped to [span_start, span_end]."""
    clipped = sorted(
        (max(start, span_start), min(end, span_end))
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


def _engine_window(
    samples: deque,
    intervals: list[tuple[float, float]],
    now_monotonic: float,
) -> tuple[float, float] | None:
    """Engine counter increase and busy time over the sampled live span.

    The span runs from the oldest to the newest counter sample inside the
    live window: the counter is only known at sample times. Busy time is
    the union of decode intervals clipped to that span. A negative step
    (engine restart) counts as 0. Returns None with fewer than two samples
    covering a positive span.
    """
    points = [
        (t, v) for t, v in samples if now_monotonic - t <= LIVE_WINDOW_S
    ]
    if len(points) < 2:
        return None
    span_start, span_end = points[0][0], points[-1][0]
    if span_end <= span_start:
        return None
    tokens = 0.0
    for (_, v0), (_, v1) in zip(points, points[1:]):
        delta = v1 - v0
        if math.isfinite(delta) and delta > 0:
            tokens += delta
    return tokens, _busy_between(intervals, span_start, span_end)


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


def _time_split_report(
    prefill_s: float, decode_s: float, requests: int,
) -> dict:
    """Prompt vs generation time over requests with both samples.

    Shares are fractions of prefill_s + decode_s; null before any paired
    request (or when the paired time is zero).
    """
    total = prefill_s + decode_s
    has_time = requests > 0 and total > 0
    return {
        "prefill_s": prefill_s,
        "decode_s": decode_s,
        "prefill_share": (prefill_s / total) if has_time else None,
        "generation_share": (decode_s / total) if has_time else None,
        "requests": requests,
    }


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
    paired_prefill_time: float = 0.0,
    paired_decode_time: float = 0.0,
    paired_requests: int = 0,
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
        # Engine counter, else calibrated streamed chunks, per busy second.
        "live": {
            "tps": 0.0, "window_s": LIVE_WINDOW_S, "estimate": True,
            "source": "estimate", "busy_s": 0.0, "tokens_per_chunk": None,
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
        "time_split": _time_split_report(
            paired_prefill_time, paired_decode_time, paired_requests,
        ),
    }
