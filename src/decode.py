"""Server-reported decode timing and per-request generation-speed samples.

Backends report their own generation (decode) work in different places:

- llama.cpp and TabbyAPI: top-level ``timings`` with ``predicted_n``
  (generated tokens) and ``predicted_ms`` (decode time).
- TabbyAPI: ``usage.completion_time`` (seconds).
- vLLM (``--enable-per-request-metrics``): top-level
  ``metrics.generation_time_ms`` (first to last token).

Without server timing, streaming requests fall back to the proxy's own
first-chunk → end time, but only when the output arrived in at least two
chunks: a buffered stream (for example a TabbyAPI tool call, emitted whole
in the final chunk) would otherwise report a near-zero decode time.

Speed is tokens / time (N, not N − 1), like the proxy measure. This module
never raises on malformed input; invalid values are ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.prefill import parse_count, parse_duration

# Fewer generated-output chunks than this means the stream was buffered.
MIN_TIMED_CHUNKS = 2


@dataclass
class ServerDecode:
    """Server decode fields seen in one response (last valid value wins)."""

    predicted_n: int | None = None
    predicted_ms: float | None = None
    completion_time_s: float | None = None
    generation_time_ms: float | None = None


@dataclass(frozen=True)
class DecodeSample:
    """Generated tokens and the seconds their decode took."""

    tokens: int
    seconds: float
    server_timed: bool


@dataclass(frozen=True)
class DecodeDecision:
    """Outcome of decode sample selection for one request."""

    sample: DecodeSample | None = None
    # True when a stream had no server timing and too few output chunks
    # for the proxy's own decode time to mean anything.
    skipped_buffered: bool = False


def apply_server_decode(data: Any, server: ServerDecode) -> None:
    """Copy valid server decode fields from a parsed response/event dict."""
    if not isinstance(data, dict):
        return
    timings = data.get("timings")
    if isinstance(timings, dict):
        # A valid pair from one event only; never mix events.
        predicted_n = parse_count(timings.get("predicted_n"))
        predicted_ms = parse_duration(timings.get("predicted_ms"))
        if predicted_n is not None and predicted_ms is not None:
            server.predicted_n = predicted_n
            server.predicted_ms = predicted_ms
    usage = data.get("usage")
    if isinstance(usage, dict):
        completion_time = parse_duration(usage.get("completion_time"))
        if completion_time is not None:
            server.completion_time_s = completion_time
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        generation_ms = parse_duration(metrics.get("generation_time_ms"))
        if generation_ms is not None:
            server.generation_time_ms = generation_ms


def select_decode(
    completion_tokens: Any,
    server: ServerDecode | None,
    proxy_decode_s: float | None,
    gen_chunks: int | None = None,
) -> DecodeDecision:
    """Pick the decode sample for one successful request.

    Priority: ``timings.predicted_n / predicted_ms`` > completion tokens /
    ``usage.completion_time`` > completion tokens / vLLM
    ``metrics.generation_time_ms`` > completion tokens / proxy decode time.
    The proxy time needs ``gen_chunks >= 2`` (``None`` = not checked);
    otherwise the request is skipped as buffered. Zero server times fall
    through to the next source.
    """
    completion = parse_count(completion_tokens)
    if completion is None or completion < 1:
        return DecodeDecision()
    if server is not None:
        if (
            server.predicted_n is not None and server.predicted_n >= 1
            and server.predicted_ms is not None and server.predicted_ms > 0
        ):
            return DecodeDecision(DecodeSample(
                tokens=server.predicted_n,
                seconds=server.predicted_ms / 1000.0,
                server_timed=True,
            ))
        seconds = None
        if server.completion_time_s:
            seconds = server.completion_time_s
        elif server.generation_time_ms:
            seconds = server.generation_time_ms / 1000.0
        if seconds is not None:
            return DecodeDecision(DecodeSample(
                tokens=completion, seconds=seconds, server_timed=True,
            ))
    if proxy_decode_s is None:
        return DecodeDecision()
    if gen_chunks is not None and gen_chunks < MIN_TIMED_CHUNKS:
        return DecodeDecision(skipped_buffered=True)
    return DecodeDecision(DecodeSample(
        tokens=completion, seconds=proxy_decode_s, server_timed=False,
    ))


def decode_interval(
    sample: DecodeSample,
    started_at: float | None,
    first_token_at: float | None,
    ended_at: float,
) -> tuple[float, float] | None:
    """Busy interval of one decode sample, or None when degenerate.

    Proxy-timed: [first token, end]. Server-timed: the server time ending
    at the request end, clamped to the request start when known.
    """
    if isinstance(ended_at, bool) or not isinstance(ended_at, (int, float)):
        return None
    if sample.server_timed:
        start = ended_at - sample.seconds
        if started_at is not None:
            start = max(started_at, start)
    else:
        start = first_token_at
    if (
        start is None
        or not math.isfinite(start)
        or not math.isfinite(ended_at)
        or ended_at <= start
    ):
        return None
    return start, ended_at
