"""Server-reported prefill timing and per-request prompt-speed samples.

Backends report their own prompt (prefill) work in different places:

- llama.cpp and TabbyAPI: top-level ``timings`` with ``prompt_n`` (new,
  uncached prompt tokens), ``prompt_ms`` (prefill time) and ``cache_n``.
- TabbyAPI: ``usage.prompt_time`` (seconds).
- TensorFold: top-level ``tensorfold`` object with ``prefill_seconds`` (MLX)
  or ``prefill_s`` (CUDA), in seconds, on the non-stream body and the
  stream's finish chunk. Read into the same slot as ``usage.prompt_time``.
- vLLM (``--enable-per-request-metrics``): top-level
  ``metrics.time_to_first_token_ms``.

This module parses those fields and picks one prefill sample per request.
It never raises on malformed input; invalid values are ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class ServerPrefill:
    """Server prefill fields seen in one response (last valid value wins)."""

    prompt_n: int | None = None
    prompt_ms: float | None = None
    cache_n: int | None = None
    prompt_time_s: float | None = None
    ttft_ms: float | None = None


@dataclass(frozen=True)
class PrefillSample:
    """Prompt work (uncached tokens) and the seconds it took."""

    tokens: int
    seconds: float
    server_timed: bool


@dataclass(frozen=True)
class PrefillDecision:
    """Outcome of sample selection for one request."""

    sample: PrefillSample | None = None
    # True when the request would have counted but its cached count was
    # unknown on a model that has never reported one.
    skipped_unknown_cache: bool = False


def parse_count(value: Any) -> int | None:
    """Return a non-negative plain int, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def parse_duration(value: Any) -> float | None:
    """Return a finite non-negative number as float, else None.

    Integers too large for a float are invalid (None), not an error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def apply_server_prefill(data: Any, server: ServerPrefill) -> None:
    """Copy valid server prefill fields from a parsed response/event dict."""
    if not isinstance(data, dict):
        return
    timings = data.get("timings")
    if isinstance(timings, dict):
        # A valid pair from one event only; never mix events.
        prompt_n = parse_count(timings.get("prompt_n"))
        prompt_ms = parse_duration(timings.get("prompt_ms"))
        if prompt_n is not None and prompt_ms is not None:
            server.prompt_n = prompt_n
            server.prompt_ms = prompt_ms
        cache_n = parse_count(timings.get("cache_n"))
        if cache_n is not None:
            server.cache_n = cache_n
    usage = data.get("usage")
    if isinstance(usage, dict):
        prompt_time = parse_duration(usage.get("prompt_time"))
        if prompt_time is not None:
            server.prompt_time_s = prompt_time
    tensorfold = data.get("tensorfold")
    if isinstance(tensorfold, dict):
        # Zero or invalid (e.g. MLX ``null`` before prefill ends) is ignored.
        for key in ("prefill_seconds", "prefill_s"):
            prefill_s = parse_duration(tensorfold.get(key))
            if prefill_s:
                server.prompt_time_s = prefill_s
                break
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        ttft_ms = parse_duration(metrics.get("time_to_first_token_ms"))
        if ttft_ms is not None:
            server.ttft_ms = ttft_ms


def effective_cached(
    cached_tokens: Any, server: ServerPrefill | None,
) -> int | None:
    """Cached prompt tokens: usage details first, else ``timings.cache_n``."""
    cached = parse_count(cached_tokens)
    if cached is not None:
        return cached
    if server is not None:
        return server.cache_n
    return None


def select_prefill(
    prompt_tokens: Any,
    cached_tokens: int | None,
    server: ServerPrefill | None,
    proxy_ttft_s: float | None,
    *,
    cache_learned: bool,
) -> PrefillDecision:
    """Pick the prefill sample for one request.

    Priority: ``timings.prompt_n / prompt_ms`` > ``usage.prompt_time`` or
    TensorFold ``tensorfold.prefill_seconds`` / ``prefill_s`` (one slot) >
    vLLM ``metrics.time_to_first_token_ms`` > proxy TTFT. For all but the
    first, tokens are prompt − cached; an unknown cached count counts as 0
    when the model has reported one before (``cache_learned``), otherwise
    the request is skipped. ``cached_tokens`` must already be resolved via
    :func:`effective_cached`.
    """
    if server is not None:
        if (
            server.prompt_n is not None and server.prompt_n >= 1
            and server.prompt_ms is not None and server.prompt_ms > 0
        ):
            return PrefillDecision(PrefillSample(
                tokens=server.prompt_n,
                seconds=server.prompt_ms / 1000.0,
                server_timed=True,
            ))
    seconds: float | None = None
    server_timed = False
    if server is not None and server.prompt_time_s:
        seconds, server_timed = server.prompt_time_s, True
    elif server is not None and server.ttft_ms:
        seconds, server_timed = server.ttft_ms / 1000.0, True
    elif proxy_ttft_s is not None:
        seconds = proxy_ttft_s
    if seconds is None:
        return PrefillDecision()
    prompt = parse_count(prompt_tokens)
    if prompt is None or prompt < 1:
        return PrefillDecision()
    if cached_tokens is None:
        if not cache_learned:
            return PrefillDecision(skipped_unknown_cache=True)
        cached_tokens = 0
    tokens = max(prompt - cached_tokens, 0)
    if tokens < 1:
        return PrefillDecision()
    return PrefillDecision(PrefillSample(
        tokens=tokens, seconds=seconds, server_timed=server_timed,
    ))


def prefill_interval(
    sample: PrefillSample,
    started_at: float,
    first_token_at: float | None,
    ended_at: float | None,
) -> tuple[float, float] | None:
    """Busy interval of one prefill sample, or None when degenerate.

    Proxy-timed: [start, first token]. Server-timed with a first token:
    the server time ending at the first token, clamped to the start.
    Server-timed without a first token (non-streaming): from the start,
    capped at ended_at.
    """
    if not sample.server_timed:
        start, end = started_at, first_token_at
    elif first_token_at is not None:
        start = max(started_at, first_token_at - sample.seconds)
        end = first_token_at
    else:
        start, end = started_at, started_at + sample.seconds
        if ended_at is not None:
            end = min(end, ended_at)
    if (
        end is None
        or not math.isfinite(start)
        or not math.isfinite(end)
        or end <= start
    ):
        return None
    return start, end
