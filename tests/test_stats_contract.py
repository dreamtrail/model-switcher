from __future__ import annotations

import ast
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient

from tests.helpers import _make_upstream_with_config
from tests.stats_helpers import _stack


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


