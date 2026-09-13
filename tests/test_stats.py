"""Statistics tests for plan P-20260913-01.

Covers the plan's §Testing strategy:
- Unit: StatsCollector percentile math, accounting, throughput windows,
  snapshot shape / null rules, inflight peak.
- Integration: /stats and /stats/json endpoints (local intercept, shadowing),
  proxied request recording (buffered/streaming/failures), rejections
  (400/404/503/504), post-drain failure path (AC12), swap/lifecycle metrics,
  concurrency peak.

Expected behavior is derived from the plan's metric definitions and the
/stats/json contract — not from the implementation.
"""

from __future__ import annotations

import asyncio
import ast
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.config import Config, ModelConfig
from src.stats import STATS_HTML, Outcome, StatsCollector, _percentile
from src.swapper import MAX_QUEUE, Swapper

from tests.helpers import _fake_handle, _make_upstream_with_config


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def _make_app(chat_handler=None):
    """Upstream app with health + chat routes (chat handler replaceable)."""
    async def handle_health(request):
        return web.json_response({"status": "ok"})

    async def default_chat(request):
        body = await request.json()
        if body.get("stream"):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(b'data: {"chunk": 1}\n\n')
            await resp.write_eof()
            return resp
        return web.json_response({"choices": [], "id": "test"})

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_post(
        "/v1/chat/completions", chat_handler or default_chat
    )
    return app


@asynccontextmanager
async def _stack(
    monkeypatch,
    upstream_apps: dict[str, web.Application] | None = None,
    *,
    seed: str | None = None,
    extra_models: tuple[str, ...] = (),
    timeout: float = 60.0,
    idle_timeout: float = 900.0,
):
    """Start upstream TestServers + a Swapper + a catch-all proxy server.

    Patches stop_process/start_process in src.swapper with fakes so no real
    subprocesses are spawned (established pattern from the idle-unload tests).
    Optionally pre-seeds an active, healthy handle for `seed` to bypass
    process startup (pattern from tests/test_swapper_swap_flow.py).

    Yields SimpleNamespace(swapper, servers, proxy).
    """
    import src.swapper as swapper_module

    async def fake_stop_process(handle):
        handle.process.returncode = 0

    async def fake_start_process(model_name, cmd, port):
        return _fake_handle(model_name, port=port)

    monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
    monkeypatch.setattr(swapper_module, "start_process", fake_start_process)

    config = Config(models={})
    servers: dict[str, TestServer] = {}
    try:
        for name, app in (upstream_apps or {}).items():
            server = TestServer(app)
            await server.start_server()
            servers[name] = server
            config.models[name] = ModelConfig(
                name=name, cmd=f"echo {name} --port ${{PORT}}", port=server.port
            )
        for name in extra_models:
            config.models[name] = ModelConfig(
                name=name, cmd=f"echo {name} --port 19876", port=19876
            )

        swapper = Swapper(config, timeout=timeout, idle_timeout=idle_timeout)

        if seed is not None:
            handle = _fake_handle(seed, port=servers[seed].port)
            swapper._current = handle
            swapper._current_model = seed
            swapper._healthy = True

        proxy_app = web.Application()

        async def handler(request):
            return await swapper.handle_request(request)

        proxy_app.router.add_route("*", "/{path:.*}", handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        try:
            yield SimpleNamespace(
                swapper=swapper, servers=servers, proxy=proxy_server
            )
        finally:
            await swapper.shutdown()
            await proxy_server.close()
    finally:
        for server in servers.values():
            await server.close()


async def _wait_for_queue(swapper, timeout: float = 2.0) -> None:
    """Wait until a queued request future shows up during a fake swap."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not swapper._queue and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert swapper._queue, "request was not queued during swap"


# ---------------------------------------------------------------------------
# Unit tests: StatsCollector
# ---------------------------------------------------------------------------


class TestPercentileMath:
    """Nearest-rank semantics per plan: sorted[ceil(q*n)-1] clamped to range."""

    def test_odd_length_known_list(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(samples, 0.50) == 30.0  # ceil(2.5)-1 = 2
        assert _percentile(samples, 0.95) == 50.0  # ceil(4.75)-1 = 4
        assert _percentile(samples, 1.00) == 50.0  # ceil(5)-1 = 4 (max)

    def test_even_length_known_list(self):
        samples = [1.0, 2.0, 3.0, 4.0]
        assert _percentile(samples, 0.50) == 2.0  # ceil(2)-1 = 1
        assert _percentile(samples, 0.95) == 4.0  # ceil(3.8)-1 = 3

    def test_single_sample(self):
        samples = [7.0]
        assert _percentile(samples, 0.50) == 7.0
        assert _percentile(samples, 0.95) == 7.0
        assert _percentile(samples, 1.00) == 7.0

    def test_empty_list_returns_zero(self):
        assert _percentile([], 0.50) == 0.0

    def test_q_zero_clamps_to_first_element(self):
        # ceil(0)-1 = -1 must clamp to index 0.
        assert _percentile([5.0, 6.0, 7.0], 0.0) == 5.0

    def test_n_ten_p95_is_last_element(self):
        samples = [float(i) for i in range(10)]
        assert _percentile(samples, 0.95) == 9.0  # ceil(9.5)-1 = 9


class TestStatsCollector:
    def test_success_failure_and_error_kind_accounting(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.1)
        collector.record_request("m", ok=True, latency_s=0.2)
        collector.record_request("m", ok=False, kind="500", latency_s=0.3)

        report = collector.snapshot()["requests"]
        assert report["total"] == 3
        assert report["success"] == 2
        assert report["failure"] == 1
        assert report["success_rate"] == pytest.approx(2 / 3)
        assert report["errors"] == {"500": 1}

    def test_ok_false_without_kind_counts_failure_but_no_error_entry(self):
        collector = StatsCollector()
        collector.record_request(None, ok=False)

        report = collector.snapshot()["requests"]
        assert report["total"] == 1
        assert report["failure"] == 1
        assert report["errors"] == {}

    def test_rejection_without_model_is_global_only(self):
        collector = StatsCollector()
        collector.record_rejection("400")

        snap = collector.snapshot()
        assert snap["requests"]["total"] == 1
        assert snap["requests"]["errors"] == {"400": 1}
        assert snap["requests"]["success"] == 0
        assert snap["models"] == {}

    def test_rejection_with_model_is_attributed(self):
        collector = StatsCollector()
        collector.record_rejection("404", model="ghost")

        snap = collector.snapshot()
        row = snap["models"]["ghost"]
        assert row["requests"]["total"] == 1
        assert row["requests"]["errors"] == {"404": 1}
        assert row["requests"]["success"] == 0
        assert row["requests"]["latency_ms"] is None  # rejections not proxied
        # Global accounting mirrors the attribution.
        assert snap["requests"]["total"] == 1
        assert snap["requests"]["errors"] == {"404": 1}

    def test_swap_aggregates_null_when_zero(self):
        snap = StatsCollector().snapshot()
        assert snap["swaps"] == {
            "count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None
        }

    def test_swap_count_and_duration_aggregates(self):
        collector = StatsCollector()
        collector.record_swap(1.0)
        collector.record_swap(2.0)
        collector.record_swap(4.0)

        swaps = collector.snapshot()["swaps"]
        assert swaps["count"] == 3
        assert swaps["avg_ms"] == pytest.approx(7000.0 / 3)
        assert swaps["p95_ms"] == pytest.approx(4000.0)  # nearest rank
        assert swaps["max_ms"] == pytest.approx(4000.0)

    def test_model_start_stop_counters(self):
        collector = StatsCollector()
        collector.record_model_start("m1")
        collector.record_model_start("m1")
        collector.record_model_stop("m1")
        collector.record_model_stop("never-started")

        models = collector.snapshot()["models"]
        assert models["m1"]["starts"] == 2
        assert models["m1"]["stops"] == 1
        assert models["never-started"]["stops"] == 1
        assert models["never-started"]["starts"] == 0

    def test_throughput_windows_and_pruning(self):
        collector = StatsCollector()
        now = time.monotonic()
        # Chronological order (oldest first): pruning pops from the left.
        collector._request_times.extend([
            now - 4000.0,  # older than 3600s retention -> pruned
            now - 400.0,   # retained but outside both windows
            now - 100.0,   # last_300s only
            now - 20.0,    # last_60s
            now - 10.0,    # last_60s
        ])

        snap = collector.snapshot()
        assert snap["throughput"]["last_60s"] == 2
        assert snap["throughput"]["last_300s"] == 3
        # The >3600s entry is pruned by snapshot.
        assert len(collector._request_times) == 4

    def test_avg_rpm_matches_total_over_uptime(self):
        collector = StatsCollector()
        for _ in range(6):
            collector.record_request(None, ok=True)

        snap = collector.snapshot()
        expected = 6 / (snap["uptime_seconds"] / 60.0)
        assert snap["throughput"]["avg_rpm"] == pytest.approx(
            expected, rel=0.01
        )

    def test_snapshot_fresh_shape_and_null_rules(self):
        snap = StatsCollector().snapshot(current_model="llama", inflight=2)

        assert set(snap) == {
            "uptime_seconds", "current_model", "concurrency", "requests",
            "throughput", "swaps", "models",
        }
        assert snap["current_model"] == "llama"
        assert snap["uptime_seconds"] >= 0.0
        assert snap["concurrency"] == {"active": 2, "peak": 0}
        assert set(snap["requests"]) == {
            "total", "success", "failure", "success_rate", "errors",
            "latency_ms", "bytes_in", "bytes_out",
        }
        assert snap["requests"]["total"] == 0
        assert snap["requests"]["success"] == 0
        assert snap["requests"]["failure"] == 0
        assert snap["requests"]["success_rate"] is None  # total == 0
        assert snap["requests"]["errors"] == {}
        assert snap["requests"]["latency_ms"] is None  # no samples
        assert snap["requests"]["bytes_in"] == 0
        assert snap["requests"]["bytes_out"] == 0
        assert snap["throughput"] == {
            "last_60s": 0, "last_300s": 0, "avg_rpm": 0.0,
        }
        assert snap["swaps"]["count"] == 0
        assert snap["models"] == {}

    def test_latency_report_known_samples(self):
        collector = StatsCollector()
        for latency in (0.1, 0.2, 0.3, 0.4):
            collector.record_request("m", ok=True, latency_s=latency)

        report = collector.snapshot()["requests"]["latency_ms"]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx(250.0)
        assert report["p50"] == pytest.approx(200.0)  # ceil(2)-1 = 1
        assert report["p95"] == pytest.approx(400.0)  # ceil(3.8)-1 = 3
        assert report["max"] == pytest.approx(400.0)

    def test_latency_per_model_matches_global_samples(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.5)
        collector.record_request(None, ok=True, latency_s=0.1)

        snap = collector.snapshot()
        assert snap["requests"]["latency_ms"]["count"] == 2
        assert snap["models"]["m"]["requests"]["latency_ms"]["count"] == 1
        assert snap["models"]["m"]["requests"]["latency_ms"]["max"] == \
            pytest.approx(500.0)

    def test_bytes_accumulate_global_and_per_model(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, bytes_in=10, bytes_out=100)
        collector.record_request("m", ok=True, bytes_in=10, bytes_out=100)
        collector.record_request(None, ok=True, bytes_in=10, bytes_out=100)

        snap = collector.snapshot()
        assert snap["requests"]["bytes_in"] == 30
        assert snap["requests"]["bytes_out"] == 300
        assert snap["models"]["m"]["requests"]["bytes_in"] == 20
        assert snap["models"]["m"]["requests"]["bytes_out"] == 200

    def test_observe_inflight_tracks_peak_only(self):
        collector = StatsCollector()
        collector.observe_inflight(3)
        collector.observe_inflight(2)  # decrease must not lower peak
        collector.observe_inflight(5)
        assert collector.snapshot()["concurrency"]["peak"] == 5

    def test_outcome_defaults(self):
        outcome = Outcome()
        assert outcome.stream_error is False
        assert outcome.bytes_out == 0
        outcome.stream_error = True
        outcome.bytes_out += 5
        assert outcome.stream_error is True
        assert outcome.bytes_out == 5


# ---------------------------------------------------------------------------
# Integration: endpoints (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


class TestStatsEndpoints:

    async def test_get_stats_returns_html_dashboard(self, monkeypatch):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(monkeypatch, {"model-a": upstream_app}) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.get("/stats")
                assert resp.status == 200
                assert resp.content_type == "text/html"
                body = await resp.text()
                assert "Model Switcher — Statistics" in body
                assert "setInterval(refresh, 5000)" in body
                # JS fetches the JSON endpoint (plan §HTML behavior).
                assert 'fetch("/stats/json")' in body

    async def test_get_stats_json_contract_fresh(self, monkeypatch):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.get("/stats/json")
                assert resp.status == 200
                assert resp.content_type == "application/json"
                data = await resp.json()

                assert isinstance(data["uptime_seconds"], float)
                assert data["current_model"] == "model-a"
                assert data["concurrency"] == {"active": 0, "peak": 0}
                assert data["requests"]["total"] == 0
                assert data["requests"]["success_rate"] is None
                assert data["requests"]["latency_ms"] is None
                assert data["requests"]["errors"] == {}
                assert data["throughput"]["avg_rpm"] == 0.0
                assert data["swaps"] == {
                    "count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None
                }
                assert data["models"] == {}

    async def test_stats_endpoints_shadow_upstream(self, monkeypatch):
        """AC1: upstream serving /stats never gets hit — intercept is local."""
        hits = {"stats": 0, "json": 0}

        async def upstream_stats(request):
            hits["stats"] += 1
            return web.Response(
                text="<html>upstream stats</html>", content_type="text/html"
            )

        async def upstream_stats_json(request):
            hits["json"] += 1
            return web.json_response({"upstream": True})

        upstream_app, _ = _make_upstream_with_config("model-a")
        upstream_app.router.add_get("/stats", upstream_stats)
        upstream_app.router.add_get("/stats/json", upstream_stats_json)

        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.get("/stats")
                assert resp.status == 200
                assert resp.content_type == "text/html"
                assert "Model Switcher — Statistics" in await resp.text()

                resp = await client.get("/stats/json")
                assert resp.status == 200
                assert resp.content_type == "application/json"
                data = await resp.json()
                assert "requests" in data  # not the upstream body

            assert hits == {"stats": 0, "json": 0}

    async def test_metadata_and_stats_excluded_from_counts(self, monkeypatch):
        """AC3: /v1/models, /models, /stats, /stats/json are not counted."""
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                for path in ("/v1/models", "/models", "/stats", "/stats/json"):
                    resp = await client.get(path)
                    assert resp.status == 200

                snap = stack.swapper.stats.snapshot()
                assert snap["requests"]["total"] == 0
                assert snap["models"] == {}

                # A proxied request IS counted, exactly once.
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 200
                snap = stack.swapper.stats.snapshot()
                assert snap["requests"]["total"] == 1
                assert snap["models"]["model-a"]["requests"]["total"] == 1


# ---------------------------------------------------------------------------
# Integration: proxied request recording (AC3, AC4, AC5, AC6)
# ---------------------------------------------------------------------------


class TestProxiedRequestStats:

    async def test_buffered_success_recorded_exactly_once(self, monkeypatch):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                payload = json.dumps(
                    {"model": "model-a", "prompt": "hi"}
                ).encode()
                resp = await client.post(
                    "/v1/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 200
                raw = await resp.read()
                assert json.loads(raw)["id"] == "model-a"

            snap = stack.swapper.stats.snapshot(current_model="model-a")
            req = snap["requests"]
            assert req["total"] == 1
            assert req["success"] == 1
            assert req["failure"] == 0
            assert req["success_rate"] == 1.0
            assert req["errors"] == {}
            assert req["latency_ms"]["count"] == 1
            assert req["latency_ms"]["avg"] > 0
            assert req["bytes_in"] == len(payload)
            assert req["bytes_out"] == len(raw)
            # Per-model row mirrors the global record.
            row = snap["models"]["model-a"]["requests"]
            assert row["total"] == 1
            assert row["success"] == 1
            assert row["latency_ms"]["count"] == 1
            assert row["bytes_in"] == len(payload)
            assert row["bytes_out"] == len(raw)
            assert snap["throughput"]["last_60s"] == 1

    async def test_upstream_500_keyed_by_status_string(self, monkeypatch):
        async def failing_chat(request):
            return web.json_response({"error": "boom"}, status=500)

        upstream_app = _make_app(failing_chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 500

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["total"] == 1
            assert snap["requests"]["success"] == 0
            assert snap["requests"]["failure"] == 1
            assert snap["requests"]["errors"] == {"500": 1}
            row = snap["models"]["model-a"]["requests"]
            assert row["errors"] == {"500": 1}

    async def test_upstream_connection_error_keyed_502(self, monkeypatch):
        async with _stack(monkeypatch) as stack:
            # Seed a handle pointing at a dead port — no upstream server.
            handle = _fake_handle("model-a", port=19876)
            stack.swapper._current = handle
            stack.swapper._current_model = "model-a"
            stack.swapper._healthy = True

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 502
                data = await resp.json()
                assert "error" in data

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"502": 1}
            assert snap["requests"]["failure"] == 1
            assert snap["models"]["model-a"]["requests"]["errors"] == {"502": 1}

    async def test_streaming_success_records_bytes_and_one_sample(
        self, monkeypatch
    ):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                text = await resp.text()
                assert '"id": "model-a"' in text

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["success"] == 1
            assert snap["requests"]["latency_ms"]["count"] == 1
            assert snap["requests"]["bytes_out"] > 0
            row = snap["models"]["model-a"]["requests"]
            assert row["latency_ms"]["count"] == 1
            assert row["bytes_out"] > 0

    async def test_mid_stream_failure_records_stream_error(self, monkeypatch):
        """AC4: upstream crashes mid-stream after prepare() -> "stream_error".

        Uses the established pattern from
        tests/test_proxy.py::test_mid_stream_error_injects_sse_error.
        """
        async def crashing_chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(b'data: {"chunk": 1}\n\n')
            # Close the writer without write_eof to simulate a crash.
            await resp.writer.close()
            return resp

        upstream_app = _make_app(crashing_chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                # Headers were already committed — status stays 200.
                assert resp.status == 200
                text = await resp.text()
                assert '"chunk": 1' in text
                assert "error" in text.lower()  # injected SSE payload

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"stream_error": 1}
            assert snap["requests"]["success"] == 0
            assert snap["requests"]["failure"] == 1
            assert snap["requests"]["latency_ms"]["count"] == 1
            assert snap["requests"]["bytes_out"] > 0

    async def test_exception_escaping_proxy_records_error_kind(
        self, monkeypatch
    ):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async def boom(*args, **kwargs):
                raise RuntimeError("boom")

            stack.swapper._proxy_for_model = boom

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 500  # aiohttp internal error

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"error": 1}
            assert snap["requests"]["total"] == 1
            assert snap["requests"]["success"] == 0
            assert snap["requests"]["failure"] == 1
            # Recording happens in finally even on exception.
            assert snap["requests"]["latency_ms"]["count"] == 1
            assert snap["requests"]["bytes_out"] == 0


# ---------------------------------------------------------------------------
# Integration: rejections (AC8)
# ---------------------------------------------------------------------------


class TestRejectionStats:

    async def test_rejection_400_no_model_global_only(self, monkeypatch):
        async with _stack(monkeypatch, extra_models=("model-a",)) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "hi"},  # no model, nothing active
                )
                assert resp.status == 400
                data = await resp.json()
                assert "error" in data

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"400": 1}
            assert snap["requests"]["total"] == 1
            # Global-only: no per-model row is created.
            assert snap["models"] == {}

    async def test_rejection_404_unknown_model_attributed(self, monkeypatch):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "ghost", "prompt": "hi"},
                )
                assert resp.status == 404
                data = await resp.json()
                assert "error" in data

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"404": 1}
            assert snap["requests"]["total"] == 1
            assert set(snap["models"]) == {"ghost"}
            row = snap["models"]["ghost"]["requests"]
            assert row["total"] == 1
            assert row["errors"] == {"404": 1}
            assert row["latency_ms"] is None

    async def test_rejection_503_queue_full_attributed(self, monkeypatch):
        upstream_app, _ = _make_upstream_with_config("model-b")
        async with _stack(
            monkeypatch, {"model-b": upstream_app}
        ) as stack:
            swapper = stack.swapper
            # Simulate swap in progress with a full queue (TestQueueOverflow
            # pattern from tests/test_swapper_swap_flow.py).
            swapper._swapping = True
            loop = asyncio.get_running_loop()
            for _ in range(MAX_QUEUE):
                swapper._queue.append(loop.create_future())

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-b"},
                )
                assert resp.status == 503

            snap = swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"503": 1}
            assert snap["requests"]["total"] == 1
            row = snap["models"]["model-b"]["requests"]
            assert row["total"] == 1
            assert row["errors"] == {"503": 1}

    async def test_rejection_504_swap_timeout_attributed(self, monkeypatch):
        """TestSwapTimeout pattern: unreachable model -> 504, recorded."""
        async with _stack(
            monkeypatch, extra_models=("timeout-model",), timeout=0.5
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "timeout-model"},
                )
                assert resp.status == 504
                data = await resp.json()
                assert data["error"]["code"] == "swap_timeout"

            snap = stack.swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"504": 1}
            assert snap["requests"]["total"] == 1
            assert snap["requests"]["latency_ms"] is None  # never proxied
            row = snap["models"]["timeout-model"]
            assert row["requests"]["total"] == 1
            assert row["requests"]["errors"] == {"504": 1}
            # Health-timeout stop is counted (AC7).
            assert row["starts"] == 1
            assert row["stops"] == 1
            # Failed swap records NO swap count/duration (AC7: recorded only
            # through successful health-readiness).
            assert snap["swaps"] == {
                "count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None
            }


# ---------------------------------------------------------------------------
# Integration: post-drain failure path (AC12)
# ---------------------------------------------------------------------------


class TestPostDrainFailurePath:

    async def test_post_drain_unknown_model_returns_404_not_bare_500(
        self, monkeypatch
    ):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            swapper = stack.swapper
            swapper._swapping = True

            async with TestClient(stack.proxy) as client:
                task = asyncio.create_task(client.post(
                    "/v1/chat/completions",
                    json={"model": "ghost", "prompt": "hi"},
                ))
                await _wait_for_queue(swapper)
                swapper._swapping = False
                await swapper._drain_queue()

                resp = await task
                assert resp.status == 404  # not a bare 500
                data = await resp.json()
                assert data["error"]["type"] == "invalid_request_error"
                assert "Unknown model" in data["error"]["message"]

            snap = swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"404": 1}
            assert snap["requests"]["total"] == 1  # exactly once
            row = snap["models"]["ghost"]["requests"]
            assert row["total"] == 1
            assert row["errors"] == {"404": 1}

    async def test_post_drain_swap_timeout_returns_504(self, monkeypatch):
        async with _stack(
            monkeypatch, extra_models=("timeout-model",), timeout=0.5
        ) as stack:
            swapper = stack.swapper
            swapper._swapping = True

            async with TestClient(stack.proxy) as client:
                task = asyncio.create_task(client.post(
                    "/v1/chat/completions",
                    json={"model": "timeout-model"},
                ))
                await _wait_for_queue(swapper)
                swapper._swapping = False
                await swapper._drain_queue()

                resp = await task
                assert resp.status == 504  # not a bare 500
                data = await resp.json()
                assert data["error"]["code"] == "swap_timeout"

            snap = swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"504": 1}
            assert snap["requests"]["total"] == 1  # exactly once
            row = snap["models"]["timeout-model"]["requests"]
            assert row["total"] == 1
            assert row["errors"] == {"504": 1}


# ---------------------------------------------------------------------------
# Integration: swap flow and lifecycle (AC7)
# ---------------------------------------------------------------------------


class TestSwapStats:

    async def test_swap_records_count_duration_starts_and_stops(
        self, monkeypatch
    ):
        upstream_a, _ = _make_upstream_with_config("model-a")
        upstream_b, _ = _make_upstream_with_config("model-b")
        async with _stack(
            monkeypatch,
            {"model-a": upstream_a, "model-b": upstream_b},
            seed="model-a",
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-b", "prompt": "hi"},
                )
                assert resp.status == 200

            snap = stack.swapper.stats.snapshot(current_model="model-b")
            assert snap["swaps"]["count"] == 1
            assert snap["swaps"]["avg_ms"] > 0
            assert snap["swaps"]["p95_ms"] > 0
            assert snap["swaps"]["max_ms"] > 0

            old = snap["models"]["model-a"]
            assert old["stops"] == 1
            assert old["starts"] == 0  # pre-seeded, never started via collector
            assert old["requests"]["total"] == 0

            new = snap["models"]["model-b"]
            assert new["starts"] == 1
            assert new["stops"] == 0
            assert new["requests"]["total"] == 1
            assert new["requests"]["success"] == 1
            assert new["requests"]["latency_ms"]["count"] == 1

    async def test_same_model_unhealthy_restart_counts_as_swap(
        self, monkeypatch
    ):
        """AC7: same-model restarts are deliberately counted as swaps."""
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            swapper = stack.swapper
            swapper._healthy = False  # force the stop-then-start branch

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 200

            snap = swapper.stats.snapshot()
            assert snap["swaps"]["count"] == 1
            row = snap["models"]["model-a"]
            assert row["stops"] == 1
            assert row["starts"] == 1
            assert row["requests"]["total"] == 1
            assert row["requests"]["success"] == 1

    async def test_crash_recovery_counts_as_swap(self, monkeypatch):
        """AC7: crash recovery (tracked parent already exited) is a swap —
        stop-then-start still runs, swap recorded through health-readiness."""
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}
        ) as stack:
            swapper = stack.swapper
            # Seed a DEAD handle: tracked parent already exited.
            handle = _fake_handle("model-a", port=stack.servers["model-a"].port)
            handle.process.returncode = 1
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 200

            snap = swapper.stats.snapshot()
            assert snap["swaps"]["count"] == 1
            assert snap["swaps"]["max_ms"] > 0
            row = snap["models"]["model-a"]
            assert row["starts"] == 1
            assert row["stops"] == 1
            assert row["requests"]["total"] == 1
            assert row["requests"]["success"] == 1

    async def test_idle_unload_records_stop_but_shutdown_does_not(
        self, simple_config: Config, monkeypatch
    ):
        import src.swapper as swapper_module

        async def fake_stop_process(handle):
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper = Swapper(simple_config, idle_timeout=0.02)
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        try:
            swapper._schedule_idle_timer()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while swapper._current is not None and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert swapper._current is None

            snap = swapper.stats.snapshot()
            assert snap["models"]["model-a"]["stops"] == 1

            # shutdown() must not add another stop.
            await swapper.shutdown()
            snap = swapper.stats.snapshot()
            assert snap["models"]["model-a"]["stops"] == 1
        finally:
            swapper._current = None

    async def test_concurrency_peak_two_concurrent_requests(self, monkeypatch):
        async def slow_chat(request):
            await asyncio.sleep(0.3)
            return web.json_response({"choices": [], "id": "model-a"})

        upstream_app = _make_app(slow_chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                responses = await asyncio.gather(
                    client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "prompt": "one"},
                    ),
                    client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "prompt": "two"},
                    ),
                )
                assert all(r.status == 200 for r in responses)

            snap = stack.swapper.stats.snapshot()
            assert snap["concurrency"]["peak"] == 2
            assert snap["concurrency"]["active"] == 0


# ---------------------------------------------------------------------------
# Architecture boundaries (AC10, AC11)
# ---------------------------------------------------------------------------


class TestArchitectureBoundaries:

    def test_stats_module_imports_stdlib_only(self):
        """AC10: stats.py imports stdlib only — no new dependencies."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "stats.py"
        ).read_text()
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {"__future__", "math", "time", "collections",
                         "dataclasses"}

    def test_proxy_does_not_touch_stats_collector(self):
        """AC11: proxy.py communicates only via the Outcome object."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "proxy.py"
        ).read_text()
        assert "StatsCollector" not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.stats":
                imported = {alias.name for alias in node.names}
                assert imported <= {"Outcome"}
