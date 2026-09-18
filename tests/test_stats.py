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
            "tokens", "rates", "ttft_ms", "tpot_ms",
        }
        assert snap["requests"]["total"] == 0
        assert snap["requests"]["success"] == 0
        assert snap["requests"]["failure"] == 0
        assert snap["requests"]["success_rate"] is None  # total == 0
        assert snap["requests"]["errors"] == {}
        assert snap["requests"]["latency_ms"] is None  # no samples
        assert snap["requests"]["bytes_in"] == 0
        assert snap["requests"]["bytes_out"] == 0
        assert snap["requests"]["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0,
            "cache_hit_rate": None,
        }
        assert snap["requests"]["rates"] == {
            "prompt_tps": {"avg": None},
            "generation_tps": {"avg": None, "last_60s": 0.0, "last_300s": 0.0},
        }
        assert snap["requests"]["ttft_ms"] is None  # no samples
        assert snap["requests"]["tpot_ms"] is None  # no samples
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
                assert data["requests"]["tokens"] == {
                    "prompt": 0, "completion": 0, "cached": 0,
                    "cache_hit_rate": None,
                }
                assert data["requests"]["rates"] == {
                    "prompt_tps": {"avg": None},
                    "generation_tps": {
                        "avg": None, "last_60s": 0.0, "last_300s": 0.0,
                    },
                }
                assert data["requests"]["ttft_ms"] is None
                assert data["requests"]["tpot_ms"] is None
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
        async with _stack(
            monkeypatch, extra_models=("model-a",)
        ) as stack:
            # Seed a configured handle pointing at a dead port — no upstream.
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
# Integration: pre-admission rejection accounting (P-20260918-02, AC8/AC11)
# ---------------------------------------------------------------------------


class TestPreAdmissionRejectionStats:

    async def test_unknown_model_is_counted_without_queue_or_intent_change(
        self, monkeypatch
    ):
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            swapper = stack.swapper
            swapper._swapping = True
            swapper._intent_generation = 4
            swapper._desired_model = "model-a"

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "ghost", "prompt": "hi"},
                )
                assert resp.status == 404
                data = await resp.json()
                assert data["error"]["type"] == "invalid_request_error"
                assert "Unknown model" in data["error"]["message"]

            assert list(swapper._queue) == []
            assert swapper._intent_generation == 4
            assert swapper._desired_model == "model-a"
            snap = swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"404": 1}
            assert snap["models"]["ghost"]["requests"]["errors"] == {
                "404": 1
            }

    async def test_quick_off_refusal_is_counted_as_409_for_requested_model(
        self, monkeypatch
    ):
        async with _stack(
            monkeypatch, extra_models=("model-a", "model-b")
        ) as stack:
            swapper = stack.swapper
            swapper._swapping = True
            swapper._intent_generation = 8
            swapper._desired_model = "model-a"

            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", json={"model": "model-b"}
                )
                assert resp.status == 409
                assert (await resp.json())["error"]["code"] == \
                    "switch_in_progress"

            assert list(swapper._queue) == []
            assert swapper._intent_generation == 8
            assert swapper._desired_model == "model-a"
            snap = swapper.stats.snapshot()
            assert snap["requests"]["errors"] == {"409": 1}
            assert snap["models"]["model-b"]["requests"]["errors"] == {
                "409": 1
            }


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


# ---------------------------------------------------------------------------
# Unit: token metrics collector derivations (P-20260913-02, AC5, AC6, AC7)
# ---------------------------------------------------------------------------


class TestTokenMetricCollector:
    """Collector-side derivations with synthetic values — exact math here.

    Expected values are computed by hand from the plan's metric
    definitions (§Metric definitions), never from the implementation.
    """

    @staticmethod
    def _record_four_streamed(collector):
        # (prompt, completion, cached, ttft_s, decode_s) per request.
        rows = [
            (100, 10, 2, 0.5, 2.0),
            (200, 5, None, 1.0, 0.8),
            (50, 2, None, 0.25, 0.2),
            (300, 8, None, 0.75, 1.4),
        ]
        for prompt, completion, cached, ttft, decode in rows:
            collector.record_request(
                "m", ok=True, latency_s=1.0,
                prompt_tokens=prompt, completion_tokens=completion,
                cached_tokens=cached, ttft_s=ttft, decode_s=decode,
            )

    def test_ttft_percentile_math_with_synthetic_inputs(self):
        # AC4: ttft_ms is a _latency_report over TTFT seconds -> ms.
        collector = StatsCollector()
        self._record_four_streamed(collector)

        report = collector.snapshot()["requests"]["ttft_ms"]

        # ttfts sorted: [0.25, 0.5, 0.75, 1.0]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx(625.0)
        assert report["p50"] == pytest.approx(500.0)   # ceil(2)-1 = 1
        assert report["p95"] == pytest.approx(1000.0)  # ceil(3.8)-1 = 3
        assert report["max"] == pytest.approx(1000.0)

    def test_tpot_percentile_math_with_synthetic_inputs(self):
        # AC5: per-request TPOT = decode_s / (completion_tokens - 1).
        collector = StatsCollector()
        self._record_four_streamed(collector)

        report = collector.snapshot()["requests"]["tpot_ms"]

        # Per-request tpots: 2.0/9, 0.8/4, 0.2/1, 1.4/7
        #   = [0.2222, 0.2, 0.2, 0.2] sorted -> [0.2, 0.2, 0.2, 0.2222]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx((0.6 + 2.0 / 9) / 4 * 1000.0)
        assert report["p50"] == pytest.approx(200.0)
        assert report["p95"] == pytest.approx(2.0 / 9 * 1000.0)  # ceil(3.8)-1=3
        assert report["max"] == pytest.approx(2.0 / 9 * 1000.0)

    def test_tps_aggregates_are_token_weighted(self):
        # AC6: gen t/s = Σcompletion / Σdecode; prompt t/s = Σprompt / ΣTTFT.
        # Mean-of-per-request rates would give 6.74 / 575 — distinct from
        # the token-weighted expected values below.
        collector = StatsCollector()
        self._record_four_streamed(collector)

        rates = collector.snapshot()["requests"]["rates"]

        assert rates["generation_tps"]["avg"] == pytest.approx(25 / 4.4)
        assert rates["prompt_tps"]["avg"] == pytest.approx(650 / 2.5)

    def test_tps_null_with_no_contributors(self):
        # AC6: both rates null when nothing contributed; empty windows 0.0.
        rates = StatsCollector().snapshot()["requests"]["rates"]

        assert rates["prompt_tps"]["avg"] is None
        assert rates["generation_tps"]["avg"] is None
        assert rates["generation_tps"]["last_60s"] == 0.0
        assert rates["generation_tps"]["last_300s"] == 0.0

    def test_tpot_null_when_completion_tokens_below_two(self):
        # AC5: TPOT undefined for completion_tokens == 1 (no sample), but
        # >= 1 still contributes to generation t/s (AC6).
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=10, completion_tokens=1,
            ttft_s=0.2, decode_s=0.3,
        )

        req = collector.snapshot()["requests"]

        assert req["tpot_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] == pytest.approx(1 / 0.3)

    def test_zero_completion_tokens_contributes_to_no_rate(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=0, completion_tokens=0,
            ttft_s=0.5, decode_s=0.5,
        )

        req = collector.snapshot()["requests"]

        assert req["rates"]["generation_tps"]["avg"] is None
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["tpot_ms"] is None
        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0, "cache_hit_rate": None,
        }

    def test_cached_tokens_summed_when_present(self):
        # AC2/AC8: cached sums only when prompt_tokens_details is present.
        collector = StatsCollector()
        collector.record_request("m", ok=True, prompt_tokens=100,
                                 cached_tokens=30)
        collector.record_request("m", ok=True, prompt_tokens=50)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"] == {
            "prompt": 150, "completion": 0, "cached": 30,
            "cache_hit_rate": 0.2,
        }
        assert snap["models"]["m"]["requests"]["tokens"]["cached"] == 30
        assert snap["requests"]["tokens"]["cache_hit_rate"] == 0.2
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] == 0.2

    def test_token_counters_recorded_regardless_of_status(self):
        # Plan §Metric definitions: counters over all requests where usage
        # was extractable, regardless of response status. Timing kwargs are
        # None here (swapper only passes them for successful requests).
        collector = StatsCollector()
        collector.record_request("m", ok=False, kind="stream_error",
                                 latency_s=0.2, prompt_tokens=10,
                                 completion_tokens=4, cached_tokens=1)

        req = collector.snapshot()["requests"]

        assert req["tokens"] == {
            "prompt": 10, "completion": 4, "cached": 1, "cache_hit_rate": 0.1,
        }
        assert req["errors"] == {"stream_error": 1}
        assert req["ttft_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        # The completion tokens still feed the windowed rate (decision 8).
        assert req["rates"]["generation_tps"]["last_60s"] == \
            pytest.approx(4 / 60)

    def test_streamed_ok_without_usage_contributes_no_token_metrics(self):
        # AC3 null rule: no usage -> tokens null, no t/s or TPOT
        # contributions, and recording never raises. TTFT is still
        # observable without usage (AC4: first content event seen).
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.4,
                                 ttft_s=0.1, decode_s=0.2)

        req = collector.snapshot()["requests"]

        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0, "cache_hit_rate": None,
        }
        assert req["ttft_ms"]["count"] == 1  # observable without usage
        assert req["tpot_ms"] is None  # needs usage + completion >= 2
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["last_60s"] == 0.0

    def test_record_request_with_no_usage_fields_never_raises(self):
        collector = StatsCollector()

        collector.record_request("m", ok=True)
        collector.record_request(None, ok=True)

        req = collector.snapshot()["requests"]

        assert req["total"] == 2
        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0, "cache_hit_rate": None,
        }
        assert req["ttft_ms"] is None
        assert req["tpot_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] is None

    def test_tokens_without_model_are_global_only(self):
        collector = StatsCollector()
        collector.record_request(None, ok=True, prompt_tokens=5,
                                 completion_tokens=2)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prompt"] == 5
        assert snap["requests"]["tokens"]["completion"] == 2
        assert snap["models"] == {}

    def test_token_counters_and_timing_mirror_per_model(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.3,
                                 prompt_tokens=7, completion_tokens=3,
                                 cached_tokens=1, ttft_s=0.2, decode_s=0.2)

        snap = collector.snapshot()

        row = snap["models"]["m"]["requests"]
        assert row["tokens"] == snap["requests"]["tokens"]
        assert row["ttft_ms"]["count"] == 1
        assert row["tpot_ms"]["count"] == 1
        assert row["rates"]["generation_tps"]["avg"] == pytest.approx(15.0)

    def test_token_windows_math_and_retention_pruning(self):
        # AC7: seeded _token_events in chronological order (oldest first —
        # pruning pops from the left, tests/test_stats.py convention).
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 4000.0, 100),  # older than 3600s retention -> pruned
            (now - 400.0, 50),    # retained, outside both windows
            (now - 100.0, 20),    # last_300s only
            (now - 20.0, 6),      # both windows
            (now - 10.0, 4),      # both windows
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        assert gen["last_60s"] == pytest.approx(10 / 60)
        assert gen["last_300s"] == pytest.approx(30 / 300)
        assert gen["avg"] is None  # no decode contributions
        # snapshot() pruned the >3600s event.
        assert len(collector.token_events) == 4

    def test_token_window_events_inside_window_counted(self):
        # Seeded safely inside the edges (snapshot re-reads the clock, so
        # seeding exactly at the edge would be flaky).
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 59.0, 60),    # inside last_60s
            (now - 299.0, 240),  # inside last_300s only
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        assert gen["last_60s"] == pytest.approx(1.0)   # 60 / 60
        assert gen["last_300s"] == pytest.approx(1.0)  # 300 / 300

    def test_token_window_events_just_past_edge_excluded(self):
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 60.5, 600),    # just past the 60s window...
            (now - 300.5, 3000),  # ...and just past the 300s window.
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        # Both events are outside the 60s window -> honest zero rate.
        assert gen["last_60s"] == 0.0
        # Only the 60.5s-old event is inside the 300s window; if the
        # 300.5s-old event leaked in, it would be (600+3000)/300 = 12.0.
        assert gen["last_300s"] == pytest.approx(600 / 300)

    def test_per_model_token_windows_seeded_chronologically(self):
        # AC8: per-model rows carry their own windowed rates.
        collector = StatsCollector()
        now = time.monotonic()
        ms = collector._model("m1")
        ms.token_events.extend([
            (now - 4000.0, 10),  # pruned by snapshot
            (now - 70.0, 20),    # last_300s only
            (now - 5.0, 30),     # both windows
        ])

        snap = collector.snapshot()

        gen = snap["models"]["m1"]["requests"]["rates"]["generation_tps"]
        assert gen["last_60s"] == pytest.approx(30 / 60)
        assert gen["last_300s"] == pytest.approx(50 / 300)
        assert len(ms.token_events) == 2  # pruned by snapshot
        # Global window is independent of per-model seeding.
        assert snap["requests"]["rates"]["generation_tps"]["last_60s"] == 0.0

    def test_cache_hit_rate_is_per_model_independent(self):
        # Each model's ratio derives from its own sums only.
        collector = StatsCollector()
        collector.record_request("a", ok=True, prompt_tokens=100,
                                 cached_tokens=50)   # 0.5
        collector.record_request("b", ok=True, prompt_tokens=200,
                                 cached_tokens=20)   # 0.1

        snap = collector.snapshot()

        assert snap["models"]["a"]["requests"]["tokens"]["cache_hit_rate"] \
            == 0.5
        assert snap["models"]["b"]["requests"]["tokens"]["cache_hit_rate"] \
            == 0.1
        # Global pools both models: 70 cached / 300 prompt.
        assert snap["requests"]["tokens"]["cache_hit_rate"] \
            == pytest.approx(70 / 300)

    def test_cache_hit_rate_null_without_prompt_tokens(self):
        # No usage recorded -> prompt sum 0 -> null, global and per-model.
        collector = StatsCollector()
        collector.record_request("m", ok=True)
        collector.record_request(None, ok=True)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["cache_hit_rate"] is None
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] \
            is None

    def test_cache_hit_rate_null_when_cached_without_prompt_tokens(self):
        # Denominator-0 anomaly: cached reported while prompt sum stays 0.
        # Division is guarded, not clamped — exact counts, never estimate.
        collector = StatsCollector()
        collector.record_request("m", ok=True, cached_tokens=5)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["cache_hit_rate"] is None
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] \
            is None

    def test_dashboard_html_has_cache_hit_column(self):
        # Static gate: the Token throughput table carries the new column.
        assert "<th>Cache hit %</th>" in STATS_HTML
        assert 'colspan="9"' in STATS_HTML
        assert "fmtRate(toks.cache_hit_rate)" in STATS_HTML
        assert 'fillRowless("token-models", 9)' in STATS_HTML


# ---------------------------------------------------------------------------
# Integration: token metrics through the real proxy path
# (P-20260913-02, AC1-AC5, AC7, AC8, AC9)
# ---------------------------------------------------------------------------


class TestTokenMetricsIntegration:
    """End-to-end token metrics through Swapper.handle_request.

    Timing assertions use LOOSE bounds (count / positivity) — event-loop
    noise makes exact values meaningless here; exact math lives in
    TestTokenMetricCollector.
    """

    async def test_stream_with_usage_chunk_records_counters_and_timing(
        self, monkeypatch,
    ):
        # AC1 (real swapper wiring), AC3, AC4, AC5, AC6.
        seen = {}

        async def chat(request):
            seen["body"] = await request.read()
            seen["content_length"] = request.headers.get("Content-Length")
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":12,'
                b'"completion_tokens":9,'
                b'"prompt_tokens_details":{"cached_tokens":3}}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        upstream_app = _make_app(chat)
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
                assert '"content":"Hel"' in text
                assert '"usage"' in text  # usage chunk flows to the client

                data = await (await client.get("/stats/json")).json()

        # AC1: upstream received the injected stream_options with a
        # Content-Length consistent with the mutated body.
        received = json.loads(seen["body"])
        assert received["model"] == "model-a"
        assert received["stream"] is True
        assert received["stream_options"] == {"include_usage": True}
        assert int(seen["content_length"]) == len(seen["body"])

        req = data["requests"]
        assert req["total"] == 1
        assert req["success"] == 1
        assert req["tokens"] == {
            "prompt": 12, "completion": 9, "cached": 3,
            "cache_hit_rate": 0.25,
        }
        assert req["ttft_ms"]["count"] == 1
        assert req["ttft_ms"]["avg"] > 0  # loose: event-loop noise
        assert req["tpot_ms"]["count"] == 1
        assert req["tpot_ms"]["avg"] > 0
        gen = req["rates"]["generation_tps"]
        assert gen["avg"] > 0
        assert gen["last_60s"] == pytest.approx(9 / 60)
        assert req["rates"]["prompt_tps"]["avg"] > 0
        # Per-model row mirrors the global record (AC8).
        row = data["models"]["model-a"]["requests"]
        assert row["tokens"] == {
            "prompt": 12, "completion": 9, "cached": 3, "cache_hit_rate": 0.25,
        }
        assert row["ttft_ms"]["count"] == 1
        assert row["tpot_ms"]["count"] == 1
        assert row["rates"]["generation_tps"]["avg"] > 0

    async def test_pre_first_chunk_delay_lands_in_ttft_not_decode(
        self, monkeypatch,
    ):
        # AC4/AC5/AC6 discriminator: an upstream-side wall-clock delay
        # BEFORE the first content chunk must land in the TTFT window,
        # not in the decode window. The delay is upstream asyncio.sleep
        # (real time, not CPU) so the bound is robust to event-loop noise.
        #
        # Margin math (completion_tokens=2, the TPOT minimum, maximizes the
        # swapped-wiring TPOT and thus the discriminating gap):
        # - correct wiring: ttft ~= 250 ms; decode = first token -> stream
        #   end, i.e. just the time to flush 3 tiny chunks + eof on a local
        #   TestServer (a few ms) -> tpot_ms avg << 100, gen tps >> 20.
        # - ttft/decode swapped: ttft ~= 0 (fails >= 150) and decode ~=
        #   250 ms -> tpot_ms ~= 250 (fails < 100), gen tps ~= 8 (fails > 20).
        # - decode = ended - started: ttft passes but decode ~= 250 ms ->
        #   tpot_ms ~= 250 and gen tps ~= 8, both fail.
        async def chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            # All model "thinking" time happens before the first token.
            await asyncio.sleep(0.25)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":6,'
                b'"completion_tokens":2}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()  # ends immediately: minimal decode window
            return resp

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await resp.text()

                data = await (await client.get("/stats/json")).json()

        req = data["requests"]
        # The pre-first-chunk sleep is TTFT, not decode (AC4).
        assert req["ttft_ms"]["count"] == 1
        assert req["ttft_ms"]["avg"] >= 150
        # The decode window is nearly empty, so TPOT stays tiny (AC5)
        # and the generation rate stays high (AC6).
        assert req["tpot_ms"]["count"] == 1
        assert req["tpot_ms"]["avg"] < 100
        assert req["rates"]["generation_tps"]["avg"] > 20

    async def test_stream_without_usage_records_null_tokens(self, monkeypatch):
        # AC3 null rule: upstream ignores the injection — no usage chunk.
        async def chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await resp.text()

                data = await (await client.get("/stats/json")).json()

        req = data["requests"]
        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0, "cache_hit_rate": None,
        }
        # Content events were observed, so TTFT is still captured (AC4).
        assert req["ttft_ms"]["count"] == 1
        assert req["ttft_ms"]["avg"] > 0
        assert req["tpot_ms"] is None  # AC5: needs usage + completion >= 2
        assert req["rates"]["generation_tps"]["avg"] is None  # AC6
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["last_60s"] == 0.0

    async def test_buffered_with_usage_records_counters_no_timing(
        self, monkeypatch,
    ):
        # AC2 + streaming-only rule (AC4/AC5/AC6): counters only.
        async def chat(request):
            body = await request.json()
            body["usage"] = {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 2},
            }
            return web.json_response(body)

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "x"},
                )
                assert resp.status == 200

                data = await (await client.get("/stats/json")).json()

        req = data["requests"]
        assert req["tokens"] == {
            "prompt": 7, "completion": 3, "cached": 2, "cache_hit_rate": 2 / 7,
        }
        assert req["ttft_ms"] is None  # non-streaming never contributes
        assert req["tpot_ms"] is None
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        # Counters still feed the windowed generation rate (decision 8).
        assert req["rates"]["generation_tps"]["last_60s"] == \
            pytest.approx(3 / 60)
        row = data["models"]["model-a"]["requests"]
        assert row["tokens"] == {
            "prompt": 7, "completion": 3, "cached": 2, "cache_hit_rate": 2 / 7,
        }
        assert row["ttft_ms"] is None

    async def test_failed_stream_records_counters_not_timing(self, monkeypatch):
        # Decision 7: timing metrics only for successful requests; token
        # counters whenever usage was extractable, regardless of status.
        async def chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":8,'
                b'"completion_tokens":4}}\n\n'
            )
            # Crash the writer without write_eof — mid-stream failure.
            await resp.writer.close()
            return resp

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200  # headers already committed
                await resp.text()

        req = stack.swapper.stats.snapshot()["requests"]
        assert req["success"] == 0
        assert req["errors"] == {"stream_error": 1}
        # Tokens recorded despite the failure.
        assert req["tokens"] == {
            "prompt": 8, "completion": 4, "cached": 0, "cache_hit_rate": 0.0,
        }
        # No timing contributions for failed requests.
        assert req["ttft_ms"] is None
        assert req["tpot_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        assert req["rates"]["prompt_tps"]["avg"] is None

    async def test_windowed_generation_tps_integration_sanity(
        self, monkeypatch,
    ):
        # AC7 integration sanity: two streamed requests with usage.
        async def chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"abc"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":10,'
                b'"completion_tokens":6}}\n\n'
            )
            await resp.write_eof()
            return resp

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                for _ in range(2):
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "stream": True},
                    )
                    assert resp.status == 200
                    await resp.text()

                data = await (await client.get("/stats/json")).json()

        gen = data["requests"]["rates"]["generation_tps"]
        # 2 requests x 6 completion tokens, all inside both windows.
        assert gen["last_60s"] == pytest.approx(12 / 60)
        assert gen["last_300s"] == pytest.approx(12 / 300)
        assert gen["avg"] > 0  # loose: decode time has event-loop noise

    async def test_stats_json_full_contract_with_usage_per_model(
        self, monkeypatch,
    ):
        # AC8: extended contract, global block and every model row.
        async def chat(request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":12,'
                b'"completion_tokens":9,'
                b'"prompt_tokens_details":{"cached_tokens":3}}}\n\n'
            )
            await resp.write_eof()
            return resp

        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await resp.text()

                data = await (await client.get("/stats/json")).json()

        req = data["requests"]
        assert set(req) == {
            "total", "success", "failure", "success_rate", "errors",
            "latency_ms", "bytes_in", "bytes_out",
            "tokens", "rates", "ttft_ms", "tpot_ms",
        }
        assert set(req["tokens"]) == {
            "prompt", "completion", "cached", "cache_hit_rate",
        }
        assert set(req["rates"]) == {"prompt_tps", "generation_tps"}
        assert set(req["rates"]["prompt_tps"]) == {"avg"}
        assert set(req["rates"]["generation_tps"]) == {
            "avg", "last_60s", "last_300s",
        }
        assert set(req["ttft_ms"]) == {"count", "avg", "p50", "p95", "max"}
        assert set(req["tpot_ms"]) == {"count", "avg", "p50", "p95", "max"}
        # Pre-existing keys and rules unchanged.
        assert req["total"] == 1
        assert req["success"] == 1
        assert req["failure"] == 0
        assert req["success_rate"] == 1.0
        assert req["errors"] == {}
        assert req["latency_ms"]["count"] == 1
        assert req["bytes_in"] > 0
        assert req["bytes_out"] > 0
        # Per-model row: same shape, same values.
        row = data["models"]["model-a"]["requests"]
        assert set(row) == set(req)
        assert row["tokens"] == req["tokens"]
        assert row["rates"] == req["rates"]
        assert row["ttft_ms"]["count"] == req["ttft_ms"]["count"]
        assert row["tpot_ms"]["count"] == req["tpot_ms"]["count"]

    async def test_dashboard_renders_token_cards_and_table(self, monkeypatch):
        # AC9: new cards + Token throughput table + unchanged polling.
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.get("/stats")
                assert resp.status == 200
                body = await resp.text()

        # Two new summary cards.
        assert 'id="gen-tps"' in body
        assert "Gen tok/s (avg)" in body
        assert 'id="completion-tokens"' in body
        assert "Completion tokens" in body
        # Token throughput table and its renderer.
        assert "Token throughput" in body
        assert 'id="token-models"' in body
        assert "renderTokenModels" in body
        for header in (
            "Prompt tok", "Compl tok", "Cached tok",
            "Prompt t/s", "Gen t/s", "TTFT p50 ms", "TTFT p95 ms",
        ):
            assert header in body
        # Null-safe "–" formatting helpers for rates and milliseconds.
        assert "fmtTps" in body
        assert "fmtMs" in body
        assert 'const DASH = "–"' in body
        # All DOM writes via textContent — no innerHTML anywhere.
        assert ".innerHTML" not in body
        assert ".textContent" in body
        # 5000 ms fetch behavior unchanged.
        assert 'fetch("/stats/json")' in body
        assert "setInterval(refresh, 5000)" in body
