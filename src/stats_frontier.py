"""In-flight request tracking and busy-time aggregate frontiers.

Finished prefill/decode intervals wait on a per-model frontier until no
in-flight request can still overlap them, then fold into the aggregate
totals. _FrontierRecorder holds the StatsCollector methods that open
and close in-flight requests.
"""

from __future__ import annotations

import math
import time

from src.decode import ServerDecode, decode_interval, select_decode
from src.prefill import ServerPrefill, effective_cached, prefill_interval
from src.stats_records import _ActiveRequest, _ModelStats
from src.stats_support import (
    _DecodeInterval,
    _PrefillInterval,
    _component_totals,
    _interval_components,
    _peak_decode_concurrency,
    _prune_live_decode_intervals,
)


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


class _FrontierRecorder:
    """StatsCollector mixin: in-flight request start/end recording.

    The host provides _models, _model(name), and _decide_prefill(...).
    """

    _models: dict[str, _ModelStats]

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
        _finalize_prefill_frontier(stats)
        # The request no longer blocks decode groups either.
        _finalize_decode_frontier(stats)

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
        _finalize_decode_frontier(stats, ending=request_id)
