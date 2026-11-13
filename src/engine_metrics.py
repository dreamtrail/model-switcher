"""Engine generated-token counter: Prometheus parsing and a decode poller.

While a model has an active decode, ``EngineCounterPoller`` reads the
backend's ``/metrics`` about once per second and hands the generated-token
counter to ``StatsCollector.record_engine_tokens``. The live generation
rate then uses the counter instead of the streamed-chunk estimate. A
backend without the endpoint or counter is probed once per model process
and then left alone (the estimate stays in use).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from src.stats import StatsCollector

logger = logging.getLogger(__name__)

# Generated-token counters that rise during generation, in lookup order.
# The first name present in the exposition is used, summed over all its
# label sets. vLLM's counter grows every engine step (verified live
# 2026-11-14). SGLang's sglang:generation_tokens_total and llama.cpp's
# llamacpp:tokens_predicted_total only grow when a request finishes, so
# they would read 0 then spike; those backends keep the chunk estimate.
ENGINE_TOKEN_COUNTERS = (
    "vllm:generation_tokens_total",
)

POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 1.0
# Upper bound on a /metrics body; larger responses count as unavailable.
MAX_METRICS_BYTES = 4 * 1024 * 1024


def _split_sample(line: str) -> tuple[str, str] | None:
    """Split one exposition line into (metric name, value text)."""
    size = len(line)
    index = 0
    while index < size and line[index] != "{" and not line[index].isspace():
        index += 1
    name = line[:index]
    while index < size and line[index].isspace():
        index += 1
    if index < size and line[index] == "{":
        quoted = escaped = False
        for index in range(index + 1, size):
            char = line[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif char == "}" and not quoted:
                break
        else:
            return None  # unterminated label set
        index += 1
    fields = line[index:].split()
    if not name or not fields:
        return None
    return name, fields[0]


def parse_generated_tokens(
    text: str, names: tuple[str, ...] = ENGINE_TOKEN_COUNTERS,
) -> float | None:
    """Sum the first known counter in a Prometheus text exposition.

    Comments, blank lines, labels, timestamps, and scientific notation are
    handled. Non-finite or negative samples are skipped. Returns None when
    no listed counter has a usable sample.
    """
    totals: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sample = _split_sample(line)
        if sample is None:
            continue
        name, value_text = sample
        if name not in names:
            continue
        try:
            value = float(value_text)
        except ValueError:
            continue
        if not math.isfinite(value) or value < 0:
            continue
        totals[name] = totals.get(name, 0.0) + value
    for name in names:
        if name in totals:
            return totals[name]
    return None


# /metrics answers that mean "this process has no metrics endpoint".
_NO_ENDPOINT_STATUSES = frozenset({404, 405, 501})


class _Missing:
    """Fetch result: the endpoint or counter does not exist."""


MISSING = _Missing()


class EngineCounterPoller:
    """Poll one model process's generated-token counter during decodes.

    At most one poll is in flight. Polling starts on ``notify_decode`` and
    stops when the model has no active decode, so an idle switcher sends
    no requests. A process whose first answer shows no endpoint or no
    counter is not polled again until a new process is loaded; timeouts
    and other errors are retried at the normal cadence.
    """

    def __init__(
        self,
        stats: StatsCollector,
        *,
        interval: float = POLL_INTERVAL_S,
        timeout: float = POLL_TIMEOUT_S,
    ) -> None:
        self._stats = stats
        self._interval = interval
        self._timeout = timeout
        self._task: asyncio.Task | None = None
        self._task_key: tuple[str, object] | None = None
        self._task_url: str | None = None
        self._closed = False
        # Process handle last probed per model, and whether it has a counter.
        self._handles: dict[str, object] = {}
        self._available: dict[str, bool | None] = {}

    def notify_decode(self, model: str, handle: object, base_url: str) -> None:
        """A decode started on ``model``; poll its counter while it runs."""
        if self._handles.get(model) is not handle:
            # New model process: forget old samples and probe again.
            self._handles[model] = handle
            self._available[model] = None
            self._stats.clear_engine_tokens(model)
        if self._available.get(model) is False or self._closed:
            return
        key = (model, handle)
        task = self._task
        if task is not None and not task.done():
            if self._task_key == key:
                # Running, or finishing: the done callback restarts it if
                # this decode is still active then.
                return
            task.cancel()
        self._start(model, handle, base_url.rstrip("/") + "/metrics")

    def _start(self, model: str, handle: object, url: str) -> None:
        # Polling restarts from idle: samples from before the idle gap
        # would mix idle time into the first engine reading.
        self._stats.clear_engine_tokens(model)
        self._task_key = (model, handle)
        self._task_url = url
        task = asyncio.create_task(self._run(model, handle, url))
        task.add_done_callback(self._on_done)
        self._task = task

    def _on_done(self, task: asyncio.Task) -> None:
        """Restart if a decode began while the loop was shutting down."""
        if task is not self._task or task.cancelled() or self._closed:
            return
        if task.result() is not True:
            return  # stopped for no counter, a new process, or an error
        model, handle = self._task_key
        if (
            self._handles.get(model) is handle
            and self._available.get(model) is not False
            and self._stats.has_active_decode(model)
        ):
            self._start(model, handle, self._task_url)

    async def _fetch(
        self, session: aiohttp.ClientSession, url: str,
    ) -> float | _Missing | None:
        """Counter value, MISSING (no endpoint/counter), or None (error)."""
        try:
            async with session.get(url) as resp:
                if resp.status in _NO_ENDPOINT_STATUSES:
                    return MISSING
                if resp.status != 200:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_METRICS_BYTES:
                        return MISSING
                    chunks.append(chunk)
                value = parse_generated_tokens(
                    b"".join(chunks).decode("utf-8", errors="replace"),
                )
                return MISSING if value is None else value
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

    async def _run(self, model: str, handle: object, url: str) -> bool:
        """Poll while decoding; True when it stopped because idle."""
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            # Own short-lived session: polls never share the proxy pool.
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while self._stats.has_active_decode(model):
                    if self._handles.get(model) is not handle:
                        return False
                    value = await self._fetch(session, url)
                    if isinstance(value, float):
                        self._available[model] = True
                        self._stats.record_engine_tokens(model, value)
                    elif value is MISSING and not self._available.get(model):
                        # No endpoint or counter on this process.
                        self._available[model] = False
                        logger.debug(
                            "No generated-token counter at %s; "
                            "live rate stays an estimate", url,
                        )
                        return False
                    # Errors (timeout, refused, 5xx): retry next interval.
                    await asyncio.sleep(self._interval)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # never let polling break the switcher
            logger.debug("Engine counter poll failed", exc_info=True)
            return False

    async def close(self) -> None:
        """Cancel any running poll loop."""
        self._closed = True
        task = self._task
        self._task = None
        self._task_key = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
