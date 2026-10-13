"""Model variants under one public name (P-20261013-01).

Routing, control API, state, and stats through the real Swapper and proxy
with in-process upstream servers; process start/stop are faked (the
real-process run lives in tests/test_variants_e2e.py).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.config import Config, ModelConfig, VariantGroup
from src.control import CONTROL_HEADER
from src.swapper import Swapper
from tests.helpers import _fake_handle


HEADERS = {CONTROL_HEADER: "1"}
IDS = ("plain", "qwen@text", "qwen@vision")


def _upstream(identity: str) -> web.Application:
    """Upstream that reports who served the request and the model it saw."""
    async def health(request):
        return web.json_response({"status": "ok"})

    async def chat(request):
        body = await request.json()
        return web.json_response(
            {"served_by": identity, "model": body.get("model")}
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/v1/chat/completions", chat)
    return app


@asynccontextmanager
async def _variant_stack(monkeypatch, **swapper_kwargs):
    """Swapper + proxy over upstreams for a plain model and two variants.

    ``gates[id]`` (an asyncio.Event) may be set by a test before a start to
    hold that start until the event is set; ``fail`` lists ids whose start
    raises.
    """
    import src.swapper as swapper_module

    events: list[tuple[str, str]] = []
    gates: dict[str, asyncio.Event] = {}
    fail: set[str] = set()

    async def fake_start(model_name, cmd, port):
        events.append(("start", model_name))
        gate = gates.get(model_name)
        if gate is not None:
            await gate.wait()
        if model_name in fail:
            raise OSError("cannot start")
        return _fake_handle(model_name, port=port)

    async def fake_stop(handle):
        events.append(("stop", handle.model_name))
        handle.process.returncode = 0

    monkeypatch.setattr(swapper_module, "start_process", fake_start)
    monkeypatch.setattr(swapper_module, "stop_process", fake_stop)

    servers = {}
    config = Config(models={}, variants={
        "qwen": VariantGroup(["text", "vision"], "text", "text"),
    })
    for model_id in IDS:
        server = TestServer(_upstream(model_id))
        await server.start_server()
        servers[model_id] = server
        config.models[model_id] = ModelConfig(
            model_id, f"echo {model_id} --port ${{PORT}}", server.port
        )
    swapper = Swapper(
        config, timeout=10, idle_timeout=0, **swapper_kwargs
    )

    proxy_app = web.Application()

    async def handler(request):
        return await swapper.handle_request(request)

    proxy_app.router.add_route("*", "/{path:.*}", handler)
    proxy = TestServer(proxy_app)
    await proxy.start_server()
    try:
        async with TestClient(proxy) as client:
            try:
                yield SimpleNamespace(
                    swapper=swapper, client=client, events=events,
                    gates=gates, fail=fail,
                )
            finally:
                # Release held starts before the client closes the server
                # (which waits for in-flight handlers), so a failing test
                # cannot hang teardown.
                for gate in gates.values():
                    gate.set()
    finally:
        await swapper.shutdown()
        await proxy.close()
        for server in servers.values():
            await server.close()


async def _chat(client, model: str):
    response = await client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    return response.status, await response.json()


async def _put(client, body: dict):
    response = await client.put(
        "/api/v1/control/model", json=body, headers=HEADERS
    )
    return response.status, await response.json()


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)


class TestRouting:
    async def test_models_list_shows_public_names_only(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            response = await stack.client.get("/v1/models")
            data = await response.json()
        assert [item["id"] for item in data["data"]] == ["plain", "qwen"]

    async def test_public_name_starts_default_variant(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            status, data = await _chat(stack.client, "qwen")
            assert status == 200
            # Body forwarded unchanged: upstream sees the public name.
            assert data == {"served_by": "qwen@text", "model": "qwen"}
            assert stack.swapper.active_model == "qwen@text"
            assert stack.events == [("start", "qwen@text")]

    async def test_internal_variant_id_from_client_is_404(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            status, data = await _chat(stack.client, "qwen@text")
            assert status == 404
            assert data["error"]["message"] == "Unknown model: qwen@text"
            assert stack.events == []
            status, _ = await _put(stack.client, {"model": "qwen@vision"})
            assert status == 404
            assert stack.events == []
            # Counted globally, never in the real variant's stats row.
            snapshot = stack.swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"404": 1}
            assert "qwen@text" not in snapshot["models"]

    async def test_plain_model_routes_as_before(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            status, data = await _chat(stack.client, "plain")
            assert (status, data["served_by"]) == (200, "plain")
            status, data = await _chat(stack.client, "qwen")
            assert (status, data["served_by"]) == (200, "qwen@text")
            assert stack.events == [
                ("start", "plain"), ("stop", "plain"), ("start", "qwen@text"),
            ]


class TestControlVariantSwitch:
    async def test_switch_stops_old_starts_new_and_reroutes_public_name(
        self, monkeypatch
    ):
        async with _variant_stack(monkeypatch) as stack:
            assert (await _chat(stack.client, "qwen"))[1]["served_by"] == (
                "qwen@text"
            )
            status, state = await _put(
                stack.client, {"model": "qwen", "variant": "vision"}
            )
            assert status == 200
            assert state["active_model"] == "qwen@vision"
            assert state["models"] == ["plain", "qwen"]
            assert state["variants"] == {"qwen": {
                "variants": ["text", "vision"],
                "default": "text",
                "active": "vision",
            }}
            # The client keeps sending the public name.
            status, data = await _chat(stack.client, "qwen")
            assert data == {"served_by": "qwen@vision", "model": "qwen"}
            assert stack.events == [
                ("start", "qwen@text"),
                ("stop", "qwen@text"),
                ("start", "qwen@vision"),
            ]
            # One stats row per variant id.
            models = stack.swapper.stats.snapshot()["models"]
            assert models["qwen@text"]["requests"]["total"] == 1
            assert models["qwen@vision"]["requests"]["total"] == 1
            assert models["qwen@text"]["loads"]["count"] == 1
            assert models["qwen@vision"]["loads"]["count"] == 1
            assert "qwen" not in models

    async def test_model_alone_loads_active_variant(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            stack.swapper.config.variants["qwen"].active = "vision"
            status, state = await _put(stack.client, {"model": "qwen"})
            assert status == 200
            assert state["active_model"] == "qwen@vision"
            assert state["variants"]["qwen"]["active"] == "vision"

    async def test_selecting_loaded_variant_is_a_no_op(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            await _chat(stack.client, "qwen")
            status, state = await _put(
                stack.client, {"model": "qwen", "variant": "text"}
            )
            assert status == 200
            assert state["active_model"] == "qwen@text"
            assert stack.events == [("start", "qwen@text")]

    @pytest.mark.parametrize(
        ("body", "status", "message", "code"),
        [
            ({"model": "qwen", "variant": "audio"}, 404,
             "Unknown variant for model qwen: audio", "unknown_variant"),
            ({"model": "plain", "variant": "text"}, 400,
             "Model plain has no variants", "model_has_no_variants"),
            ({"model": "nope", "variant": "text"}, 404,
             "Unknown model: nope", "unknown_model"),
            ({"model": "qwen", "variant": ""}, 400,
             "Invalid control request", "invalid_control_request"),
            ({"model": "qwen", "variant": 1}, 400,
             "Invalid control request", "invalid_control_request"),
            ({"model": "qwen", "variant": None}, 400,
             "Invalid control request", "invalid_control_request"),
            ({"model": "qwen", "variant": "text", "x": 1}, 400,
             "Invalid control request", "invalid_control_request"),
            ({"variant": "text"}, 400,
             "Invalid control request", "invalid_control_request"),
        ],
    )
    async def test_invalid_variant_requests_change_nothing(
        self, monkeypatch, body, status, message, code
    ):
        async with _variant_stack(monkeypatch) as stack:
            got_status, data = await _put(stack.client, body)
            assert got_status == status
            assert data["error"]["message"] == message
            assert data["error"]["code"] == code
            assert stack.swapper.config.variants["qwen"].active == "text"
            assert stack.events == []

    async def test_state_lists_variants(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            response = await stack.client.get("/api/v1/control/state")
            state = await response.json()
        assert state == {
            "models": ["plain", "qwen"],
            "variants": {"qwen": {
                "variants": ["text", "vision"],
                "default": "text",
                "active": "text",
            }},
            "active_model": None,
            "pending_model": None,
            "lifecycle": "idle",
            "inflight_requests": 0,
            "settings": {
                "automatic_switch": True,
                "quick_switch": False,
                "idle_timeout_seconds": 0.0,
            },
        }


class TestVariantChoiceAtomicity:
    async def test_choice_is_published_with_admission_and_requests_join(
        self, monkeypatch
    ):
        async with _variant_stack(monkeypatch) as stack:
            swapper = stack.swapper
            gate = stack.gates["qwen@vision"] = asyncio.Event()
            load = asyncio.create_task(
                swapper.control_load_model("qwen", "vision")
            )
            # One loop step: admission and the variant choice happen before
            # the first await inside the load.
            await asyncio.sleep(0)
            assert swapper.config.variants["qwen"].active == "vision"
            assert swapper.config.resolve("qwen") == "qwen@vision"
            assert swapper.control_state()["pending_model"] == "qwen@vision"
            # Without quick switch a different target would get 409; the
            # public name now resolves to the loading variant and joins it.
            request = asyncio.create_task(_chat(stack.client, "qwen"))
            await _wait_until(lambda: len(swapper._queue) == 1)
            gate.set()
            assert await load == "success"
            status, data = await request
            assert (status, data["served_by"]) == (200, "qwen@vision")
            assert stack.events == [("start", "qwen@vision")]

    async def test_refused_admission_keeps_previous_choice(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            swapper = stack.swapper
            gate = stack.gates["qwen@text"] = asyncio.Event()
            request = asyncio.create_task(_chat(stack.client, "qwen"))
            await _wait_until(
                lambda: swapper._desired_model == "qwen@text"
            )
            outcome = await swapper.control_load_model("qwen", "vision")
            assert outcome == "switch_in_progress"
            assert swapper.config.variants["qwen"].active == "text"
            gate.set()
            status, data = await request
            assert (status, data["served_by"]) == (200, "qwen@text")

    async def test_quick_switch_supersedes_old_variant_request(
        self, monkeypatch
    ):
        async with _variant_stack(monkeypatch, quick_switch=True) as stack:
            swapper = stack.swapper
            gate = stack.gates["qwen@text"] = asyncio.Event()
            request = asyncio.create_task(_chat(stack.client, "qwen"))
            await _wait_until(
                lambda: swapper._desired_model == "qwen@text"
            )
            load = asyncio.create_task(
                swapper.control_load_model("qwen", "vision")
            )
            await asyncio.sleep(0)
            assert swapper.config.variants["qwen"].active == "vision"
            gate.set()
            assert await load == "success"
            status, data = await request
            assert status == 409
            assert data["error"]["code"] == "swap_superseded"
            status, data = await _chat(stack.client, "qwen")
            assert (status, data["served_by"]) == (200, "qwen@vision")

    async def test_failed_load_keeps_the_choice(self, monkeypatch):
        async with _variant_stack(monkeypatch) as stack:
            stack.fail.add("qwen@vision")
            status, data = await _put(
                stack.client, {"model": "qwen", "variant": "vision"}
            )
            assert status == 500
            assert data["error"]["code"] == "model_start_failed"
            assert stack.swapper.config.variants["qwen"].active == "vision"
            loads = stack.swapper.stats.snapshot()["models"]["qwen@vision"]
            assert loads["loads"]["failed"]["model_start_failed"] == 1
