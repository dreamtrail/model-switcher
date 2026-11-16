"""Stats record types: the proxy Outcome and per-model accumulators.

Also holds _apply_usage, the usage/timing arithmetic shared by the
global mirrors on StatsCollector and each _ModelStats.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from src.decode import DecodeDecision, ServerDecode
from src.prefill import PrefillDecision, ServerPrefill
from src.stats_support import (
    GEN_CALIBRATION_SAMPLES,
    _AggregateTotals,
    _DecodeInterval,
    _PrefillInterval,
    _duration_report,
    _prune_token_events,
)


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


@dataclass
class _ActiveRequest:
    """An in-flight proxied request whose prefill may still be unpublished."""

    started_at: float
    first_token_at: float | None = None


@dataclass
class _LoadStats:
    """Model loads: one process start attempt each (start -> healthy)."""

    durations: list[float] = field(default_factory=list)  # successful, s
    stop_durations: list[float] = field(default_factory=list)  # old model, s
    swaps: int = 0  # successful loads that stopped a previous model first
    failed: dict[str, int] = field(default_factory=dict)
    abandoned: dict[str, int] = field(default_factory=dict)

    def record(
        self, load_s: float, stop_s: float | None, swap: bool,
    ) -> None:
        self.durations.append(load_s)
        if stop_s is not None:
            self.stop_durations.append(stop_s)
        if swap:
            self.swaps += 1

    def record_outcome(self, bucket: str, kind: str) -> None:
        counts = self.failed if bucket == "failed" else self.abandoned
        counts[kind] = counts.get(kind, 0) + 1

    def report(self) -> dict:
        return {
            **_duration_report(self.durations),
            "swaps": self.swaps,
            "stop": _duration_report(self.stop_durations),
            "failed": dict(self.failed),
            "abandoned": dict(self.abandoned),
        }


@dataclass
class _ModelStats:
    """Per-model counters and samples."""

    starts: int = 0
    stops: int = 0
    loads: _LoadStats = field(default_factory=_LoadStats)
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
    # Time split: only requests with both a prefill and a decode sample.
    paired_prefill_time: float = 0.0  # Σ prefill seconds of paired requests
    paired_decode_time: float = 0.0  # Σ decode seconds of paired requests
    paired_requests: int = 0  # requests with both samples
    # Learned: this model has reported a cached-token count at least once.
    cache_reported: bool = False
    # (monotonic, tokens)
    token_events: deque = field(default_factory=deque)
    # (monotonic, units)
    gen_chunk_events: deque = field(default_factory=deque)
    active_decodes: dict[object, float] = field(default_factory=dict)
    # Live rate: recently ended decodes (started_at, ended_at), any outcome.
    live_decode_intervals: deque = field(default_factory=deque)
    # Live rate: (completion_tokens, chunks) of recent finished streams.
    gen_calibration: deque = field(
        default_factory=lambda: deque(maxlen=GEN_CALIBRATION_SAMPLES),
    )
    # Live rate: engine generated-token counter samples (monotonic, total)
    # from src/engine_metrics.py; cleared when a new process is probed.
    engine_samples: deque = field(default_factory=deque)
    decode_frontier: list[_DecodeInterval] = field(default_factory=list)
    max_aggregate_tps: float | None = None
    max_aggregate_concurrency: int | None = None
    decode_aggregate: _AggregateTotals = field(
        default_factory=_AggregateTotals,
    )
    active_requests: dict[object, _ActiveRequest] = field(default_factory=dict)
    prefill_frontier: list[_PrefillInterval] = field(default_factory=list)
    prefill_aggregate: _AggregateTotals = field(
        default_factory=_AggregateTotals,
    )


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
    if gen is not None and sample is not None:
        # Time split counts a request only when both sides are known.
        stats.paired_prefill_time += sample.seconds
        stats.paired_decode_time += gen.seconds
        stats.paired_requests += 1
    events = stats.token_events
    _prune_token_events(events, now_monotonic)
