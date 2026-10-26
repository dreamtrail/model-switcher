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
from src.stats_page import STATS_HTML
from src.stats_support import (
    GEN_CALIBRATION_SAMPLES,
    LIVE_WINDOW_S,
    REQUEST_RETENTION_S,
    WINDOW_LONG_S,
    WINDOW_SHORT_S,
    _AggregateTotals,
    _DecodeInterval,
    _PrefillInterval,
    _busy_seconds,
    _component_totals,
    _duration_report,
    _interval_components,
    _is_count,
    _peak_decode_concurrency,
    _percentile,
    _prune_gen_chunk_events,
    _prune_live_decode_intervals,
    _prune_token_events,
    _requests_report,
    _tokens_per_chunk,
)

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


class StatsCollector:
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
        if gen is not None and sample is not None:
            # Time split counts a request only when both sides are known.
            stats.paired_prefill_time += sample.seconds
            stats.paired_decode_time += gen.seconds
            stats.paired_requests += 1
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
                "loads": ms.loads.report(),
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
                    paired_prefill_time=ms.paired_prefill_time,
                    paired_decode_time=ms.paired_decode_time,
                    paired_requests=ms.paired_requests,
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
                paired_prefill_time=self.paired_prefill_time,
                paired_decode_time=self.paired_decode_time,
                paired_requests=self.paired_requests,
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
            "loads": self._loads.report(),
            "models": models,
            "utilization": self._utilization_report(now_monotonic),
        }
