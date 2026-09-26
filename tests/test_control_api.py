"""Loopback HTTP contracts for the runtime control API (P-20260923-01)."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.control import CONTROL_HEADER
from tests.helpers import _make_upstream_with_config
from tests.stats_helpers import _stack


CONTROL_HEADERS = {CONTROL_HEADER: "1"}
ERROR_BASE = {"type": "invalid_request_error", "param": None}
SENSITIVE_STATE_FIELDS = (
    "cmd",
    "command",
    "port",
    "pid",
    "process",
    "environment",
    "config_path",
    "yaml",
)


def assert_no_cors_permission(response) -> None:
    assert not any(
        name.lower().startswith("access-control-allow")
        for name in response.headers
    )


def assert_error(data: dict, *, message: str, code: str | None) -> None:
    assert data == {"error": {"message": message, "code": code, **ERROR_BASE}}


async def assert_exact_state_response(response, expected: dict) -> dict:
    """Assert the complete success contract, not selected response fields."""
    data = await response.json()
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert_no_cors_permission(response)
    assert data == expected
    serialized = json.dumps(data).lower()
    for forbidden in SENSITIVE_STATE_FIELDS:
        assert forbidden not in serialized
    return data


class TestStateAndReservedRoutes:
    async def test_state_has_exact_schema_order_defaults_and_no_sensitive_data(
        self, monkeypatch
    ):
        async with _stack(
            monkeypatch,
            extra_models=("model-a", "model-b"),
            idle_timeout=37.5,
        ) as stack:
            async with TestClient(stack.proxy) as client:
                response = await client.get("/api/v1/control/state")
                data = await response.json()

        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert data == {
            "models": ["model-a", "model-b"],
            "active_model": None,
            "pending_model": None,
            "lifecycle": "idle",
            "inflight_requests": 0,
            "settings": {
                "automatic_switch": True,
                "quick_switch": False,
                "idle_timeout_seconds": 37.5,
            },
        }
        serialized = json.dumps(data).lower()
        for forbidden in SENSITIVE_STATE_FIELDS:
            assert forbidden not in serialized

    @pytest.mark.parametrize(
        ("path", "method", "allow"),
        [
            ("/control", "POST", "GET"),
            ("/api/v1/control/state", "PUT", "GET"),
            ("/api/v1/control/settings", "GET", "PUT"),
            ("/api/v1/control/model", "POST", "PUT, DELETE"),
        ],
    )
    async def test_recognized_route_unsupported_method_has_exact_local_405(
        self, monkeypatch, path, method, allow
    ):
        hits = []

        async def upstream_conflict(request):
            hits.append((request.method, request.path))
            return web.json_response({"upstream": True})

        upstream, _ = _make_upstream_with_config("model-a")
        upstream.router.add_route("*", path, upstream_conflict)
        async with _stack(
            monkeypatch, {"model-a": upstream}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                response = await client.request(method, path)
                data = await response.json()

        assert response.status == 405
        assert response.headers["Allow"] == allow
        assert_error(data, message="Method not allowed", code="method_not_allowed")
        assert hits == []

    @pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
    @pytest.mark.parametrize(
        "path", ["/api/v1/control", "/api/v1/control/missing", "/api/v1/control/x/y"]
    )
    async def test_unknown_reserved_route_has_local_404_without_allow(
        self, monkeypatch, method, path
    ):
        hits = []

        async def upstream_conflict(request):
            hits.append(request.path)
            return web.json_response({"upstream": True})

        upstream, _ = _make_upstream_with_config("model-a")
        upstream.router.add_route("*", "/{tail:.*}", upstream_conflict)
        async with _stack(
            monkeypatch, {"model-a": upstream}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                response = await client.request(method, path)
                data = await response.json()

        assert response.status == 404
        assert "Allow" not in response.headers
        assert_error(
            data, message="Control route not found", code="control_route_not_found"
        )
        assert hits == []

    async def test_reserved_success_routes_shadow_conflicting_upstream(
        self, monkeypatch
    ):
        hits = []

        async def upstream_conflict(request):
            hits.append(request.path)
            return web.json_response({"upstream": True})

        upstream, _ = _make_upstream_with_config("model-a")
        for path in ("/control", "/api/v1/control/state"):
            upstream.router.add_get(path, upstream_conflict)
        async with _stack(
            monkeypatch, {"model-a": upstream}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                panel = await client.get("/control")
                state = await client.get("/api/v1/control/state")
                assert panel.status == state.status == 200
                assert "<title>Model Switcher — Control</title>" in await panel.text()
                assert (await state.json())["active_model"] == "model-a"
        assert hits == []


class TestSettingsValidation:
    async def test_all_successful_api_operations_return_exact_safe_state_without_cors(
        self, monkeypatch
    ):
        upstream, _ = _make_upstream_with_config("a")
        async with _stack(
            monkeypatch, {"a": upstream}, idle_timeout=90
        ) as stack:
            settings = {
                "automatic_switch": False,
                "quick_switch": True,
                "idle_timeout_seconds": 0.0,
            }
            base = {
                "models": ["a"],
                "active_model": None,
                "pending_model": None,
                "lifecycle": "idle",
                "inflight_requests": 0,
                "settings": settings,
            }
            origin = {"Origin": "https://cross-origin.invalid"}
            mutation_headers = {**CONTROL_HEADERS, **origin}
            async with TestClient(stack.proxy) as client:
                panel = await client.get("/control", headers=origin)
                initial = await client.get(
                    "/api/v1/control/state", headers=origin
                )
                settings_response = await client.put(
                    "/api/v1/control/settings",
                    json=settings,
                    headers=mutation_headers,
                )
                model_response = await client.put(
                    "/api/v1/control/model",
                    json={"model": "a"},
                    headers=mutation_headers,
                )
                delete_response = await client.delete(
                    "/api/v1/control/model", headers=mutation_headers
                )

                assert panel.status == 200
                assert_no_cors_permission(panel)
                await assert_exact_state_response(
                    initial,
                    {
                        **base,
                        "settings": {
                            "automatic_switch": True,
                            "quick_switch": False,
                            "idle_timeout_seconds": 90.0,
                        },
                    },
                )
                await assert_exact_state_response(settings_response, base)
                await assert_exact_state_response(
                    model_response,
                    {
                        **base,
                        "active_model": "a",
                        "lifecycle": "ready",
                    },
                )
                await assert_exact_state_response(delete_response, base)

    async def test_valid_settings_are_atomic_immediate_and_idempotent(
        self, monkeypatch
    ):
        async with _stack(monkeypatch, extra_models=("a",), idle_timeout=90) as stack:
            async with TestClient(stack.proxy) as client:
                payload = {
                    "automatic_switch": False,
                    "quick_switch": True,
                    "idle_timeout_seconds": 0,
                }
                first = await client.put(
                    "/api/v1/control/settings",
                    json=payload,
                    headers=CONTROL_HEADERS,
                )
                second = await client.put(
                    "/api/v1/control/settings",
                    json=payload,
                    headers=CONTROL_HEADERS,
                )
                first_data, second_data = await first.json(), await second.json()

        assert first.status == second.status == 200
        assert first.headers["Cache-Control"] == second.headers["Cache-Control"] == "no-store"
        assert first_data == second_data
        assert first_data["settings"] == payload
        assert stack.swapper.automatic_switch is False
        assert stack.swapper.quick_switch is True
        assert stack.swapper.idle_timeout == 0.0

    @pytest.mark.parametrize(
        ("body", "content_type", "status", "code"),
        [
            ('{"automatic_switch":true,"quick_switch":false}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":false,"idle_timeout_seconds":1,"extra":0}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":1,"quick_switch":false,"idle_timeout_seconds":1}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":0,"idle_timeout_seconds":1}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":false,"idle_timeout_seconds":true}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":false,"idle_timeout_seconds":-0.1}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":false,"idle_timeout_seconds":NaN}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"quick_switch":false,"idle_timeout_seconds":Infinity}', "application/json", 400, "invalid_control_request"),
            ('{"automatic_switch":true,"automatic_switch":false,"quick_switch":false,"idle_timeout_seconds":1}', "application/json", 400, "invalid_control_request"),
            ('[true,false,1]', "application/json", 400, "invalid_control_request"),
            ('{"broken"', "application/json", 400, "invalid_control_request"),
            ('{}', "text/plain", 415, "unsupported_media_type"),
        ],
        ids=[
            "missing-key", "unknown-key", "automatic-not-bool", "quick-not-bool",
            "timeout-bool", "timeout-negative", "timeout-nan", "timeout-infinity",
            "duplicate-key", "non-object", "malformed", "wrong-media-type",
        ],
    )
    async def test_invalid_settings_never_partially_mutate(
        self, monkeypatch, body, content_type, status, code
    ):
        async with _stack(monkeypatch, extra_models=("a",), idle_timeout=91) as stack:
            before = stack.swapper.control_state()["settings"].copy()
            async with TestClient(stack.proxy) as client:
                response = await client.put(
                    "/api/v1/control/settings",
                    data=body,
                    headers={**CONTROL_HEADERS, "Content-Type": content_type},
                )
                data = await response.json()
            after = stack.swapper.control_state()["settings"]

        assert response.status == status
        assert data["error"]["code"] == code
        assert before == after

    async def test_oversized_settings_body_is_413_without_mutation(
        self, monkeypatch
    ):
        async with _stack(monkeypatch, extra_models=("a",)) as stack:
            before = stack.swapper.control_state()["settings"].copy()
            async with TestClient(stack.proxy) as client:
                response = await client.put(
                    "/api/v1/control/settings",
                    data=b"{" + b" " * 4096,
                    headers={**CONTROL_HEADERS, "Content-Type": "application/json"},
                )
                data = await response.json()
            assert stack.swapper.control_state()["settings"] == before

        assert response.status == 413
        assert_error(
            data,
            message="Control request body is too large",
            code="control_request_too_large",
        )


class TestMutationBoundaryAndAutomaticPolicy:
    @pytest.mark.parametrize(
        ("path", "requested_method", "requested_headers", "allow"),
        [
            (
                "/api/v1/control/settings",
                "PUT",
                "X-Model-Switcher-Control, Content-Type",
                "PUT",
            ),
            (
                "/api/v1/control/model",
                "PUT",
                "X-Model-Switcher-Control, Content-Type",
                "PUT, DELETE",
            ),
            (
                "/api/v1/control/model",
                "DELETE",
                "X-Model-Switcher-Control",
                "PUT, DELETE",
            ),
        ],
    )
    async def test_realistic_preflight_is_local_405_without_cors_or_mutation(
        self,
        monkeypatch,
        path,
        requested_method,
        requested_headers,
        allow,
    ):
        hits = []

        async def upstream_conflict(request):
            hits.append((request.method, request.path))
            return web.json_response({"upstream": True})

        upstream, _ = _make_upstream_with_config("a")
        upstream.router.add_route("*", "/api/v1/control/settings", upstream_conflict)
        upstream.router.add_route("*", "/api/v1/control/model", upstream_conflict)
        async with _stack(
            monkeypatch, {"a": upstream}, seed="a", idle_timeout=0
        ) as stack:
            before = stack.swapper.control_state()
            async with TestClient(stack.proxy) as client:
                response = await client.options(
                    path,
                    headers={
                        "Origin": "https://cross-origin.invalid",
                        "Access-Control-Request-Method": requested_method,
                        "Access-Control-Request-Headers": requested_headers,
                    },
                )
                data = await response.json()
            after = stack.swapper.control_state()

        assert response.status == 405
        assert response.headers["Allow"] == allow
        assert_error(data, message="Method not allowed", code="method_not_allowed")
        assert_no_cors_permission(response)
        assert after == before
        assert hits == []

    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("PUT", "/api/v1/control/settings", {"json": {"automatic_switch": False, "quick_switch": True, "idle_timeout_seconds": 0}}),
            ("PUT", "/api/v1/control/model", {"json": {"model": "a"}}),
            ("DELETE", "/api/v1/control/model", {}),
        ],
    )
    @pytest.mark.parametrize("header_value", [None, "0", "true"])
    async def test_missing_or_wrong_control_header_forbids_without_mutation(
        self, monkeypatch, method, path, kwargs, header_value
    ):
        async with _stack(monkeypatch, extra_models=("a",), idle_timeout=77) as stack:
            before = stack.swapper.control_state()
            headers = {} if header_value is None else {CONTROL_HEADER: header_value}
            async with TestClient(stack.proxy) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
                data = await response.json()
            after = stack.swapper.control_state()

        assert response.status == 403
        assert_error(
            data,
            message="Control mutation header is required",
            code="control_request_forbidden",
        )
        assert after == before
        assert not any(name.lower().startswith("access-control-allow") for name in response.headers)

    async def test_model_put_schema_media_size_and_delete_body_are_strict(
        self, monkeypatch
    ):
        async with _stack(monkeypatch, extra_models=("a",)) as stack:
            async with TestClient(stack.proxy) as client:
                cases = [
                    await client.put("/api/v1/control/model", json={}, headers=CONTROL_HEADERS),
                    await client.put("/api/v1/control/model", json={"model": ""}, headers=CONTROL_HEADERS),
                    await client.put("/api/v1/control/model", json={"model": "a", "x": 1}, headers=CONTROL_HEADERS),
                    await client.put("/api/v1/control/model", data="a", headers={**CONTROL_HEADERS, "Content-Type": "text/plain"}),
                    await client.put("/api/v1/control/model", data=b"{" + b" " * 4096, headers={**CONTROL_HEADERS, "Content-Type": "application/json"}),
                    await client.delete("/api/v1/control/model", data=b"x", headers=CONTROL_HEADERS),
                ]
                outcomes = [(response.status, (await response.json())["error"]["code"]) for response in cases]

        assert outcomes == [
            (400, "invalid_control_request"),
            (400, "invalid_control_request"),
            (400, "invalid_control_request"),
            (415, "unsupported_media_type"),
            (413, "control_request_too_large"),
            (400, "invalid_control_request"),
        ]
        assert stack.swapper.active_model is None
        assert stack.swapper.stats.snapshot()["requests"]["total"] == 0

    async def test_automatic_switch_gate_allows_active_and_model_less_and_manual_bypass(
        self, monkeypatch
    ):
        upstream_a, _ = _make_upstream_with_config("a")
        upstream_b, _ = _make_upstream_with_config("b")
        async with _stack(
            monkeypatch, {"a": upstream_a, "b": upstream_b}, seed="a", idle_timeout=0
        ) as stack:
            async with TestClient(stack.proxy) as client:
                setting = await client.put(
                    "/api/v1/control/settings",
                    json={"automatic_switch": False, "quick_switch": False, "idle_timeout_seconds": 0},
                    headers=CONTROL_HEADERS,
                )
                rejected = await client.post("/v1/chat/completions", json={"model": "b"})
                unknown = await client.post("/v1/chat/completions", json={"model": "ghost"})
                active = await client.post("/v1/chat/completions", json={"model": "a"})
                model_less = await client.post("/v1/chat/completions", json={"prompt": "use active"})
                manual_unknown = await client.put(
                    "/api/v1/control/model", json={"model": "ghost"}, headers=CONTROL_HEADERS
                )
                manual = await client.put(
                    "/api/v1/control/model", json={"model": "b"}, headers=CONTROL_HEADERS
                )

                assert setting.status == 200
                assert rejected.status == 409
                assert (await rejected.json())["error"]["code"] == "automatic_switch_disabled"
                assert unknown.status == 404
                assert active.status == model_less.status == 200
                assert manual_unknown.status == 404
                assert (await manual_unknown.json())["error"]["code"] == "unknown_model"
                assert manual.status == 200
                assert (await manual.json())["active_model"] == "b"

            snapshot = stack.swapper.stats.snapshot()
            assert snapshot["requests"]["total"] == 4
            assert snapshot["requests"]["errors"] == {"409": 1, "404": 1}
            assert snapshot["models"]["b"]["requests"]["errors"] == {"409": 1}
            assert snapshot["models"]["ghost"]["requests"]["errors"] == {"404": 1}
            assert snapshot["models"]["a"]["requests"]["total"] == 2

    async def test_runtime_settings_are_process_local_and_fresh_instances_restore_defaults(
        self, monkeypatch
    ):
        async with _stack(
            monkeypatch, extra_models=("a",), idle_timeout=11
        ) as first:
            initial_model_fields = {
                name: (model.cmd, model.port) for name, model in first.swapper.config.models.items()
            }
            first.swapper.control_apply_settings(
                automatic_switch=False, quick_switch=True, idle_timeout_seconds=99
            )
            assert first.swapper.control_state()["settings"] == {
                "automatic_switch": False,
                "quick_switch": True,
                "idle_timeout_seconds": 99.0,
            }
            assert {
                name: (model.cmd, model.port) for name, model in first.swapper.config.models.items()
            } == initial_model_fields

        async with _stack(
            monkeypatch, extra_models=("a",), idle_timeout=11
        ) as fresh:
            assert fresh.swapper.control_state()["settings"] == {
                "automatic_switch": True,
                "quick_switch": False,
                "idle_timeout_seconds": 11.0,
            }
