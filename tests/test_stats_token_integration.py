from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from tests.helpers import _make_upstream_with_config
from tests.stats_helpers import _make_app, _stack


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
        decode_starts = []
        decode_ends = []
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            original_start = stack.swapper.stats.record_decode_start
            original_end = stack.swapper.stats.record_decode_end

            def capture_start(model, request_id, started_at):
                decode_starts.append((model, request_id, started_at))
                original_start(model, request_id, started_at)

            def capture_end(
                model, request_id, *, ended_at, ok, completion_tokens,
            ):
                decode_ends.append(
                    (model, request_id, ended_at, ok, completion_tokens)
                )
                original_end(
                    model,
                    request_id,
                    ended_at=ended_at,
                    ok=ok,
                    completion_tokens=completion_tokens,
                )

            monkeypatch.setattr(
                stack.swapper.stats, "record_decode_start", capture_start
            )
            monkeypatch.setattr(
                stack.swapper.stats, "record_decode_end", capture_end
            )

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

        # AC3: two content events trigger one start, associated to the one
        # matching end by an opaque request identity and exact final usage.
        assert len(decode_starts) == len(decode_ends) == 1
        start_model, start_identity, started_at = decode_starts[0]
        end_model, end_identity, ended_at, end_ok, end_tokens = decode_ends[0]
        assert start_model == end_model == "model-a"
        assert start_identity is end_identity
        assert ended_at > started_at
        assert end_ok is True
        assert end_tokens == 9

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
            "prompt": 12, "completion": 9, "cached": 3, "prefill": 9,
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
            "prompt": 12, "completion": 9, "cached": 3, "prefill": 9,
            "cache_hit_rate": 0.25,
        }
        assert row["ttft_ms"]["count"] == 1
        assert row["tpot_ms"]["count"] == 1
        assert row["rates"]["generation_tps"]["avg"] > 0

    async def test_cancellation_after_first_token_cleans_up_and_propagates(
        self, monkeypatch,
    ):
        # AC3/AC4: cancellation must close the matching started identity as
        # ineligible, re-raise CancelledError, and not strand later cohorts.
        upstream_app, _ = _make_upstream_with_config("model-a")
        first_token_seen = asyncio.Event()
        calls = 0
        starts = []
        ends = []

        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            stats = stack.swapper.stats
            original_start = stats.record_decode_start
            original_end = stats.record_decode_end

            def capture_start(model, request_id, started_at):
                starts.append((model, request_id))
                original_start(model, request_id, started_at)

            def capture_end(
                model, request_id, *, ended_at, ok, completion_tokens,
            ):
                ends.append((model, request_id, ok, completion_tokens))
                original_end(
                    model,
                    request_id,
                    ended_at=ended_at,
                    ok=ok,
                    completion_tokens=completion_tokens,
                )

            monkeypatch.setattr(stats, "record_decode_start", capture_start)
            monkeypatch.setattr(stats, "record_decode_end", capture_end)

            async def controlled_proxy(
                request, model_name, body, handle, outcome,
            ):
                nonlocal calls
                calls += 1
                started_at = time.monotonic()
                outcome.first_token_at = started_at
                outcome.first_token_observer(started_at)
                if calls == 1:
                    first_token_seen.set()
                    await asyncio.Future()
                outcome.completion_tokens = 6
                await asyncio.sleep(0)
                return web.Response(status=200, body=b"ok")

            monkeypatch.setattr(
                stack.swapper, "_proxy_for_model", controlled_proxy
            )
            handle = stack.swapper._current

            stack.swapper._begin_activity()
            cancelled = asyncio.create_task(
                stack.swapper._proxy_with_recorded_activity(
                    SimpleNamespace(), "model-a", None, handle,
                )
            )
            await first_token_seen.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

            generation = stats.snapshot()["models"]["model-a"]["requests"][
                "rates"
            ]["generation_tps"]
            assert generation["max_aggregate"] is None

            stack.swapper._begin_activity()
            response = await stack.swapper._proxy_with_recorded_activity(
                SimpleNamespace(), "model-a", None, handle,
            )
            assert response.status == 200

            maximum = stats.snapshot()["models"]["model-a"]["requests"][
                "rates"
            ]["generation_tps"]["max_aggregate"]
            assert maximum["tps"] > 0
            assert maximum["concurrency"] == 1

        assert len(starts) == len(ends) == 2
        assert starts[0][1] is ends[0][1]
        assert starts[1][1] is ends[1][1]
        assert starts[0][1] is not starts[1][1]
        assert ends[0][2:] == (False, None)
        assert ends[1][2:] == (True, 6)

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
            "prompt": 0, "completion": 0, "cached": 0, "prefill": 0,
            "cache_hit_rate": None,
        }
        # Content events were observed, so TTFT is still captured (AC4).
        assert req["ttft_ms"]["count"] == 1
        assert req["ttft_ms"]["avg"] > 0
        assert req["tpot_ms"] is None  # AC5: needs usage + completion >= 2
        assert req["rates"]["generation_tps"]["avg"] is None  # AC6
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["last_60s"] == 0.0
        assert data["models"]["model-a"]["requests"]["rates"][
            "generation_tps"
        ]["max_aggregate"] is None

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
            "prompt": 7, "completion": 3, "cached": 2, "prefill": 0,
            "cache_hit_rate": 2 / 7,
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
            "prompt": 7, "completion": 3, "cached": 2, "prefill": 0,
            "cache_hit_rate": 2 / 7,
        }
        assert row["ttft_ms"] is None
        assert row["rates"]["generation_tps"]["max_aggregate"] is None

    async def test_failed_stream_records_counters_not_timing(self, monkeypatch):
        # Decision 7 and AC3/AC4: a real mid-stream transport failure keeps
        # streamed response behavior and ordinary usage counters, but closes
        # exactly the matching decode identity without an aggregate token
        # contribution. A later real stream must still be publishable.
        calls = 0

        async def chat(request):
            nonlocal calls
            calls += 1
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            content = b"Hi" if calls == 1 else b"Bye"
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"'
                + content
                + b'"}}]}\n\n'
            )
            if calls == 1:
                await resp.write(
                    b'data: {"choices":[],"usage":{"prompt_tokens":8,'
                    b'"completion_tokens":4}}\n\n'
                )
                # Crash the writer without write_eof — mid-stream failure.
                await resp.writer.close()
                return resp
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":6}}\n\n'
            )
            await resp.write_eof()
            return resp

        starts = []
        ends = []
        upstream_app = _make_app(chat)
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            stats = stack.swapper.stats
            original_start = stats.record_decode_start
            original_end = stats.record_decode_end

            def capture_start(model, request_id, started_at):
                starts.append((model, request_id, started_at))
                original_start(model, request_id, started_at)

            def capture_end(
                model, request_id, *, ended_at, ok, completion_tokens,
            ):
                ends.append(
                    (model, request_id, ended_at, ok, completion_tokens)
                )
                original_end(
                    model,
                    request_id,
                    ended_at=ended_at,
                    ok=ok,
                    completion_tokens=completion_tokens,
                )

            monkeypatch.setattr(stats, "record_decode_start", capture_start)
            monkeypatch.setattr(stats, "record_decode_end", capture_end)

            async with TestClient(stack.proxy) as client:
                failed = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert failed.status == 200  # headers already committed
                failed_text = await failed.text()
                assert '"content":"Hi"' in failed_text
                assert "error" in failed_text.lower()  # injected SSE payload

                # Exactly one observed start is matched by one ineligible end;
                # extracted usage remains on ordinary counters, never on the
                # aggregate event or retained frontier.
                assert len(starts) == len(ends) == 1
                assert starts[0][0] == ends[0][0] == "model-a"
                assert starts[0][1] is ends[0][1]
                assert ends[0][2] >= starts[0][2]
                assert ends[0][3:] == (False, None)
                model_stats = stats._models["model-a"]
                assert model_stats.active_decodes == {}
                assert model_stats.decode_frontier == []

                failed_snap = stats.snapshot()
                failed_req = failed_snap["requests"]
                assert failed_snap["models"]["model-a"]["requests"][
                    "rates"
                ]["generation_tps"]["max_aggregate"] is None

                eligible = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert eligible.status == 200
                eligible_text = await eligible.text()
                assert '"content":"Bye"' in eligible_text
                assert "error" not in eligible_text.lower()

                # A subsequent request to the real stats endpoint also gives
                # the completed stream handler's finally path a deterministic
                # scheduling boundary before observing its publication.
                published = await (await client.get("/stats/json")).json()
                maximum = published["models"]["model-a"]["requests"][
                    "rates"
                ]["generation_tps"]["max_aggregate"]
                assert maximum["tps"] > 0
                assert maximum["concurrency"] == 1

        # Preserve all prior failed-stream accounting assertions.
        assert failed_req["success"] == 0
        assert failed_req["errors"] == {"stream_error": 1}
        assert failed_req["tokens"] == {
            "prompt": 8, "completion": 4, "cached": 0, "prefill": 0,
            "cache_hit_rate": 0.0,
        }
        assert failed_req["ttft_ms"] is None
        assert failed_req["tpot_ms"] is None
        assert failed_req["rates"]["generation_tps"]["avg"] is None
        assert failed_req["rates"]["prompt_tps"]["avg"] is None

        assert len(starts) == len(ends) == 2
        assert starts[1][1] is ends[1][1]
        assert starts[0][1] is not starts[1][1]
        assert ends[1][3:] == (True, 6)

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
            "prompt", "completion", "cached", "prefill", "cache_hit_rate",
        }
        assert set(req["rates"]) == {"prompt_tps", "generation_tps"}
        assert set(req["rates"]["prompt_tps"]) == {"avg", "avg_aggregate"}
        assert set(req["rates"]["generation_tps"]) == {
            "avg", "last_60s", "last_300s", "avg_aggregate",
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
        # Per-model requests retain the global shape except for the scoped
        # maximum aggregate generation-rate field (P-20260918-05, AC6).
        row = data["models"]["model-a"]["requests"]
        assert set(row) == set(req)
        assert row["tokens"] == req["tokens"] == {
            "prompt": 12, "completion": 9, "cached": 3, "prefill": 9,
            "cache_hit_rate": 0.25,
        }
        assert set(row["rates"]["generation_tps"]) == {
            "avg", "last_60s", "last_300s", "max_aggregate", "avg_aggregate",
        }
        maximum = row["rates"]["generation_tps"]["max_aggregate"]
        assert set(maximum) == {"tps", "concurrency"}
        assert isinstance(maximum["tps"], float)
        assert maximum["tps"] > 0
        assert maximum["concurrency"] == 1
        assert {
            key: value
            for key, value in row["rates"]["generation_tps"].items()
            if key != "max_aggregate"
        } == req["rates"]["generation_tps"]
        assert row["rates"]["prompt_tps"] == req["rates"]["prompt_tps"]
        assert row["ttft_ms"]["count"] == req["ttft_ms"]["count"]
        assert row["tpot_ms"]["count"] == req["tpot_ms"]["count"]

    async def test_dashboard_renders_token_cards_and_table(self, monkeypatch):
        # AC9: token-rate columns + unchanged polling (compact page since P-20260925-01).
        upstream_app, _ = _make_upstream_with_config("model-a")
        async with _stack(
            monkeypatch, {"model-a": upstream_app}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.get("/stats")
                assert resp.status == 200
                body = await resp.text()

        # Compact per-model table (P-20260925-01) with token-rate columns;
        # P-20260925-04 adds busy-time totals and token counts.
        assert 'id="models"' in body
        assert "renderTable" in body
        for header in (
            "<th>Gen t/s</th>", ">Gen total</th>",
            "<th>Prompt t/s</th>", ">Prompt total</th>",
            "<th>TTFT p50 / p95</th>", "<th>Cache</th>",
            "<th>Gen tok</th>", "<th>Prefill tok</th>",
        ):
            assert header in body
        assert "Best gen t/s" not in body
        # Null-safe "–" formatting helpers for rates and milliseconds.
        assert "fmtTps" in body
        assert "fmtTtft" in body
        assert 'const DASH = "–"' in body
        # All DOM writes via textContent — no innerHTML anywhere.
        assert ".innerHTML" not in body
        assert ".textContent" in body
        # 5000 ms fetch behavior unchanged.
        assert 'fetch("/stats/json")' in body
        assert "setInterval(refresh, 5000)" in body
