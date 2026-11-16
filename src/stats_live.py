"""Live generation rate: per-model and global tokens/s right now."""

from __future__ import annotations

import math

from src.stats_records import _ModelStats
from src.stats_support import (
    LIVE_WINDOW_S,
    _busy_seconds,
    _engine_window,
    _tokens_per_chunk,
)

# Floor for the live-rate busy-time denominator (seconds). Kept at 1 s with
# the 5 s window: engine samples arrive about once per second, so a fresh
# decode already spans about 1 s of busy time when its rate first shows.
LIVE_BUSY_FLOOR_S = 1.0


def _live_reports(
    models: dict[str, _ModelStats],
    now_monotonic: float,
    current_model: str | None = None,
) -> tuple[dict[str, dict], dict]:
    """Per-model and global live generation rates.

    Engine source: the generated-token counter increase over the
    sampled part of the 5 s window divided by busy time in that span.
    Estimate source (no counter samples): calibrated tokens (chunks in
    the window × tokens per chunk) divided by busy time in the window.
    Busy time is the union of ended and active decode intervals,
    floored at 1 s. The rate is 0 while no decode is active. Global
    uses the current model's engine rate when it has one and no other
    model is decoding; otherwise the summed estimate.
    """
    per_model: dict[str, dict] = {}
    all_intervals: list[tuple[float, float]] = []
    total_tokens = 0.0
    any_active = False
    for name, ms in models.items():
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
        engine = _engine_window(
            ms.engine_samples, intervals, now_monotonic,
        )
        if engine is not None:
            engine_tokens, engine_busy = engine
            tps = engine_tokens / max(engine_busy, LIVE_BUSY_FLOOR_S)
            source = "engine"
        else:
            tps = tokens / max(busy, LIVE_BUSY_FLOOR_S)
            source = "estimate"
        per_model[name] = {
            "tps": tps if active else 0.0,
            "estimate": source == "estimate",
            "source": source,
            "busy_s": busy,
            "tokens_per_chunk": ratio,
        }
    busy = _busy_seconds(all_intervals, now_monotonic)
    current = per_model.get(current_model)
    others_active = any(
        ms.active_decodes
        for name, ms in models.items() if name != current_model
    )
    if (
        current is not None
        and current["source"] == "engine"
        and not others_active
    ):
        global_live = {
            "tps": current["tps"],
            "estimate": False,
            "source": "engine",
            "busy_s": busy,
            "tokens_per_chunk": None,
        }
    else:
        global_live = {
            "tps": (
                total_tokens / max(busy, LIVE_BUSY_FLOOR_S)
                if any_active else 0.0
            ),
            "estimate": True,
            "source": "estimate",
            "busy_s": busy,
            "tokens_per_chunk": None,
        }
    return per_model, global_live
