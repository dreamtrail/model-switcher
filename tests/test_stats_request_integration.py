from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient

from src.config import Config
from src.swapper import MAX_QUEUE, Swapper

from tests.helpers import _fake_handle, _make_upstream_with_config
from tests.stats_helpers import _make_app, _stack


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
        tests/test_proxy_sse.py::TestStreamProxy::test_mid_stream_error_injects_sse_error.
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


