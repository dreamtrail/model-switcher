"""Engine generated-token counter: parser and poller (P-20261114-01).

The poller tests run a real aiohttp server as the fake backend and a real
StatsCollector; only the poll interval and timeout are shortened.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from src.engine_metrics import (
    ENGINE_TOKEN_COUNTERS,
    EngineCounterPoller,
    parse_generated_tokens,
)
from src.stats import StatsCollector

VLLM_SAMPLE = """\
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0",model_name="qwen"} 2.429807e+06
vllm:generation_tokens_total{engine="1",model_name="qwen"} 100
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
vllm:prompt_tokens_total{engine="0",model_name="qwen"} 9.9e+07
vllm:generation_tokens_created{engine="0",model_name="qwen"} 1.7e+09
"""


class TestParser:
    def test_table_lists_only_live_counters(self):
        # SGLang and llama.cpp counters only grow when a request finishes.
        assert ENGINE_TOKEN_COUNTERS == ("vllm:generation_tokens_total",)

    def test_vllm_counter_summed_over_label_sets(self):
        assert parse_generated_tokens(VLLM_SAMPLE) == 2429807.0 + 100.0

    def test_unlabelled_sample_with_timestamp(self):
        text = "vllm:generation_tokens_total 42 1700000000000\n"
        assert parse_generated_tokens(text) == 42.0

    def test_quoted_label_values_with_braces_spaces_and_escapes(self):
        text = (
            'vllm:generation_tokens_total{model_name="a} b",'
            'path="x\\"}y"} 7e1\n'
            'vllm:generation_tokens_total {engine="0"} 3\n'
        )
        assert parse_generated_tokens(text) == 73.0

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "# only comments\n# TYPE vllm:generation_tokens_total counter\n",
            "llamacpp:tokens_predicted_total 500\n",
            'sglang:generation_tokens_total{model_name="m"} 500\n',
            "vllm:prompt_tokens_total 500\n",
            "vllm:generation_tokens_total NaN\n",
            "vllm:generation_tokens_total +Inf\n",
            "vllm:generation_tokens_total -5\n",
            "vllm:generation_tokens_total abc\n",
            'vllm:generation_tokens_total{engine="0" 5\n',
            "vllm:generation_tokens_total\n",
            "<html>not metrics</html>\n",
        ],
    )
    def test_missing_or_unusable_counter_is_none(self, text):
        assert parse_generated_tokens(text) is None

    def test_bad_lines_do_not_hide_good_ones(self):
        text = (
            "vllm:generation_tokens_total NaN\n"
            "garbage line here\n"
            'vllm:generation_tokens_total{engine="1"} 12\n'
        )
        assert parse_generated_tokens(text) == 12.0


class _Backend:
    """Fake engine: /metrics answers per ``mode``; requests are counted."""

    def __init__(self, mode: str = "vllm") -> None:
        self.mode = mode
        self.value = 0.0
        self.calls = 0
        self.release = asyncio.Event()
        # Modes to use for the first calls, before ``mode``.
        self.script: list[str] = []

    async def metrics(self, request: web.Request) -> web.StreamResponse:
        self.calls += 1
        mode = self.script.pop(0) if self.script else self.mode
        if mode == "missing":
            raise web.HTTPNotFound()
        if mode == "not_implemented":
            raise web.HTTPNotImplemented()
        if mode == "error":
            raise web.HTTPInternalServerError()
        if mode == "hang":
            await asyncio.wait_for(self.release.wait(), timeout=5)
        if mode == "big":
            # Over 1 MB of other metrics, the counter on the last line;
            # streamed in pieces so the client cannot get it in one read.
            self.value += 10
            resp = web.StreamResponse()
            await resp.prepare(request)
            filler = "".join(
                f'vllm:other_metric{{bucket="{i}",model_name="qwen"}} {i}\n'
                for i in range(1000)
            ).encode()
            for _ in range(25):
                await resp.write(filler)
                await asyncio.sleep(0)
            await resp.write(
                f"vllm:generation_tokens_total {self.value}\n".encode(),
            )
            await resp.write_eof()
            return resp
        if mode == "llamacpp":
            return web.Response(
                text=f"llamacpp:tokens_predicted_total {self.value}\n",
            )
        self.value += 10
        return web.Response(
            text=f'vllm:generation_tokens_total{{engine="0"}} {self.value}\n',
        )

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/metrics", self.metrics)
        return app


async def _serve(backend: _Backend) -> TestServer:
    server = TestServer(backend.app())
    await server.start_server()
    return server


async def _until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def _url(server: TestServer) -> str:
    return f"http://127.0.0.1:{server.port}"


class TestPoller:
    async def test_polls_while_decoding_and_stops_when_idle(self):
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02, timeout=1.0)
        handle = object()
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", handle, _url(server))
            # Repeated notifications reuse the running loop (one in flight).
            poller.notify_decode("m", handle, _url(server))
            await _until(lambda: backend.calls >= 3)
            samples = stats._models["m"].engine_samples
            assert len(samples) >= 3
            assert [v for _, v in samples][:3] == [10.0, 20.0, 30.0]

            stats.record_decode_end(
                "m", "r1", ended_at=1.0, ok=True, completion_tokens=None,
            )
            await _until(lambda: poller._task.done())
            idle_calls = backend.calls
            await asyncio.sleep(0.2)  # 10 intervals with nothing decoding
            assert backend.calls == idle_calls
        finally:
            await poller.close()
            await server.close()

    async def test_no_poll_when_decode_already_ended(self):
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        try:
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: poller._task.done())
            assert backend.calls == 0
        finally:
            await poller.close()
            await server.close()

    @pytest.mark.parametrize("mode", ["missing", "not_implemented", "llamacpp"])
    async def test_unavailable_counter_probed_once_per_process(self, mode):
        backend = _Backend(mode)
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        handle = object()
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", handle, _url(server))
            await _until(lambda: poller._task.done())
            assert backend.calls == 1
            assert len(stats._models["m"].engine_samples) == 0
            # Same process, new decode: no re-probe.
            poller.notify_decode("m", handle, _url(server))
            await asyncio.sleep(0.1)
            assert backend.calls == 1
            # New process (model reloaded): probe again.
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: backend.calls == 2)
        finally:
            await poller.close()
            await server.close()

    async def test_new_process_clears_old_samples(self):
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: len(stats._models["m"].engine_samples) >= 2)
            backend.mode = "missing"
            poller.notify_decode("m", object(), _url(server))
            # Cleared at once, and the new process has no counter.
            assert len(stats._models["m"].engine_samples) == 0
            await _until(lambda: poller._task.done())
            assert len(stats._models["m"].engine_samples) == 0
        finally:
            await poller.close()
            await server.close()

    async def test_hanging_metrics_bounded_by_timeout_and_retried(self):
        # A timeout is transient: it does not mark the process as having
        # no counter, and the next poll follows at the normal cadence.
        backend = _Backend("vllm")
        backend.script = ["hang"]
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02, timeout=0.2)
        loop = asyncio.get_running_loop()
        try:
            stats.record_decode_start("m", "r1", 0.0)
            started = loop.time()
            poller.notify_decode("m", object(), _url(server))
            await _until(
                lambda: len(stats._models["m"].engine_samples) >= 1,
                timeout=2.0,
            )
            assert loop.time() - started < 1.0
            assert backend.calls >= 2
            assert poller._available["m"] is True
        finally:
            backend.release.set()
            await poller.close()
            await server.close()

    @pytest.mark.parametrize("first", ["error", "refused"])
    async def test_first_probe_error_is_retried(self, first):
        backend = _Backend("vllm")
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02, timeout=0.5)
        try:
            stats.record_decode_start("m", "r1", 0.0)
            if first == "error":
                backend.script = ["error", "error"]
                url = _url(server)
            else:
                # Nothing listens on the backend's port yet.
                url = _url(server)
                await server.close()
            poller.notify_decode("m", object(), url)
            if first == "refused":
                await asyncio.sleep(0.1)
                assert not poller._task.done()
                assert poller._available["m"] is None
                return
            await _until(lambda: len(stats._models["m"].engine_samples) >= 1)
            assert backend.calls >= 3
            assert poller._available["m"] is True
        finally:
            await poller.close()
            await server.close()

    async def test_large_body_with_counter_near_the_end(self):
        backend = _Backend("big")
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02, timeout=2.0)
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: len(stats._models["m"].engine_samples) >= 2)
            assert [v for _, v in stats._models["m"].engine_samples][:2] == [
                10.0, 20.0,
            ]
            assert poller._available["m"] is True
        finally:
            await poller.close()
            await server.close()

    async def test_body_over_cap_counts_as_no_counter(self, monkeypatch):
        monkeypatch.setattr("src.engine_metrics.MAX_METRICS_BYTES", 100_000)
        backend = _Backend("big")
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: poller._task.done())
            assert poller._available["m"] is False
            assert len(stats._models["m"].engine_samples) == 0
        finally:
            await poller.close()
            await server.close()

    async def test_first_token_while_loop_is_stopping_restarts_polling(self):
        # Regression (reviewer-01): decode A ends, the loop decides to
        # stop, and decode B's first token arrives while the old task is
        # still closing its session. Polling must resume for B.
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        handle = object()
        url = _url(server)
        seen = {}
        try:
            stats.record_decode_start("m", "a", 0.0)
            poller.notify_decode("m", handle, url)
            await _until(lambda: backend.calls >= 2)
            original = stats.has_active_decode

            def spy(model):
                active = original(model)
                if not active and not seen:
                    seen["stopping"] = True

                    def start_b():
                        stats.record_decode_start("m", "b", 2.0)
                        poller.notify_decode("m", handle, url)
                        seen["task_done"] = poller._task.done()

                    asyncio.get_running_loop().call_soon(start_b)
                return active

            stats.has_active_decode = spy
            stats.record_decode_end(
                "m", "a", ended_at=1.0, ok=True, completion_tokens=None,
            )
            await _until(lambda: "task_done" in seen)
            assert seen["task_done"] is False  # the race really happened
            calls = backend.calls
            await _until(lambda: backend.calls >= calls + 3)
            assert stats.has_active_decode("m")
        finally:
            await poller.close()
            await server.close()

    async def test_restart_from_idle_drops_samples_before_the_gap(self):
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        handle = object()
        try:
            stats.record_decode_start("m", "a", 0.0)
            poller.notify_decode("m", handle, _url(server))
            await _until(lambda: len(stats._models["m"].engine_samples) >= 3)
            stats.record_decode_end(
                "m", "a", ended_at=1.0, ok=True, completion_tokens=None,
            )
            await _until(lambda: poller._task.done())
            assert len(stats._models["m"].engine_samples) >= 3
            last = stats._models["m"].engine_samples[-1][1]

            stats.record_decode_start("m", "b", 2.0)
            poller.notify_decode("m", handle, _url(server))
            # Old samples are gone at once; only decode B's samples follow.
            assert all(
                v > last for _, v in stats._models["m"].engine_samples
            )
            await _until(lambda: len(stats._models["m"].engine_samples) >= 1)
            assert stats._models["m"].engine_samples[0][1] == last + 10
        finally:
            await poller.close()
            await server.close()

    async def test_close_cancels_running_loop(self):
        backend = _Backend()
        server = await _serve(backend)
        stats = StatsCollector()
        poller = EngineCounterPoller(stats, interval=0.02)
        try:
            stats.record_decode_start("m", "r1", 0.0)
            poller.notify_decode("m", object(), _url(server))
            await _until(lambda: backend.calls >= 1)
            task = poller._task
            await poller.close()
            assert task.done()
            calls = backend.calls
            await asyncio.sleep(0.1)
            assert backend.calls == calls
        finally:
            await server.close()
