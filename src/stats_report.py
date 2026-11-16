"""Snapshot report blocks built from collected stats records.

StatsCollector.snapshot assembles the /stats/json contract from these.
"""

from __future__ import annotations

from src.stats_records import _ModelStats
from src.stats_support import (
    _AggregateTotals,
    _percentile,
    _requests_report,
)


def _swaps_report(durations: list[float]) -> dict:
    """Swap count and avg/p95/max duration in milliseconds."""
    if durations:
        ordered = sorted(durations)
        count = len(ordered)
        return {
            "count": count,
            "avg_ms": sum(ordered) / count * 1000.0,
            "p95_ms": _percentile(ordered, 0.95) * 1000.0,
            "max_ms": ordered[-1] * 1000.0,
        }
    return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}


def _usage_report(
    holder: _ModelStats,
    total: int,
    success: int,
    errors: dict[str, int],
    latencies: list[float],
    bytes_in: int,
    bytes_out: int,
    **extra: object,
) -> dict:
    """Requests block from a holder's token/timing fields.

    holder is a _ModelStats or the collector's global mirrors (same field
    names). extra passes the remaining _requests_report keywords.
    """
    return _requests_report(
        total,
        success,
        errors,
        latencies,
        bytes_in,
        bytes_out,
        prompt_tokens=holder.prompt_tokens,
        completion_tokens=holder.completion_tokens,
        cached_tokens=holder.cached_tokens,
        ttfts=holder.ttfts,
        tpots=holder.tpots,
        decode_tokens=holder.decode_tokens,
        decode_time=holder.decode_time,
        decode_server_tokens=holder.decode_server_tokens,
        decode_skipped_buffered=holder.decode_skipped_buffered,
        prefill_tokens=holder.prefill_tokens,
        prefill_time=holder.prefill_time,
        prefill_server_tokens=holder.prefill_server_tokens,
        prompt_skipped_unknown_cache=holder.prompt_skipped_unknown_cache,
        paired_prefill_time=holder.paired_prefill_time,
        paired_decode_time=holder.paired_decode_time,
        paired_requests=holder.paired_requests,
        token_events=holder.token_events,
        **extra,
    )


def _model_report(
    ms: _ModelStats, now_monotonic: float, live: dict,
) -> dict:
    """One model's entry in the snapshot "models" map."""
    return {
        "starts": ms.starts,
        "stops": ms.stops,
        "loads": ms.loads.report(),
        "requests": _usage_report(
            ms,
            ms.total,
            ms.success,
            ms.errors,
            ms.latencies,
            ms.bytes_in,
            ms.bytes_out,
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
            live=live,
        ),
    }


def _global_aggregates(
    models: dict[str, _ModelStats],
) -> tuple[_AggregateTotals, _AggregateTotals]:
    """Global decode/prefill busy-time totals: sum of per-model ones."""
    global_decode = _AggregateTotals()
    global_prefill = _AggregateTotals()
    for ms in models.values():
        global_decode.add(ms.decode_aggregate)
        global_prefill.add(ms.prefill_aggregate)
    return global_decode, global_prefill
