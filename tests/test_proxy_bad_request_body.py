"""Bad client JSON bodies count as model-less requests (P-20261116-13).

``extract_model`` must return None, not raise, for a non-UTF-8 body, a
JSON value that is not an object, or a ``model`` that is not a string.
Such requests then take the normal model-less path: 400 when no model is
running, or forwarded to the running model. The end-to-end tests send real
requests through a Swapper to a local aiohttp upstream.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, make_mocked_request

from src.proxy import extract_model

from tests.stats_helpers import _make_app, _stack

_JSON = {"Content-Type": "application/json"}

_BAD_BODIES = [
    pytest.param(b'{"model":"caf\xe9"}', id="non-utf8"),
    pytest.param(b"[1]", id="array"),
    pytest.param(b'"x"', id="string"),
    pytest.param(b"5", id="number"),
    pytest.param(b"null", id="null"),
    pytest.param(b'{"model": 5}', id="model-number"),
    pytest.param(b'{"model": [1]}', id="model-list"),
    pytest.param(b'{"model": {"a": 1}}', id="model-object"),
]


def _request():
    return make_mocked_request("POST", "/v1/chat/completions", headers=_JSON)


class TestUnitExtractModel:

    @pytest.mark.parametrize("body", _BAD_BODIES)
    def test_bad_body_returns_none(self, body):
        assert extract_model(_request(), body) is None

    def test_string_model_still_returned(self):
        assert extract_model(_request(), b'{"model": "m"}') == "m"


class TestEndToEnd:

    @pytest.mark.parametrize("body", _BAD_BODIES)
    async def test_no_model_running_gets_400(self, monkeypatch, body):
        async with _stack(
            monkeypatch, {"model-a": _make_app()}
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", data=body, headers=_JSON,
                )
                assert resp.status == 400
                data = await resp.json()
                assert "No model specified" in data["error"]["message"]
            assert stack.swapper._current is None

    @pytest.mark.parametrize("body", _BAD_BODIES)
    async def test_running_model_gets_request(self, monkeypatch, body):
        seen: list[bytes] = []

        async def chat(request):
            seen.append(await request.read())
            return web.json_response({"choices": [], "id": "ok"})

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", data=body, headers=_JSON,
                )
                assert resp.status == 200
                assert (await resp.json())["id"] == "ok"
            assert seen == [body]

    @pytest.mark.parametrize("body", [
        pytest.param(b'{"model":"caf\xe9"}', id="non-utf8"),
        pytest.param(b"[1]", id="array"),
    ])
    async def test_query_model_with_bad_body_is_forwarded(
        self, monkeypatch, body,
    ):
        seen: list[bytes] = []

        async def chat(request):
            seen.append(await request.read())
            return web.json_response({"choices": [], "id": "ok"})

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions?model=model-a",
                    data=body, headers=_JSON,
                )
                assert resp.status == 200
                assert (await resp.json())["id"] == "ok"
            assert seen == [body]


# Deeply nested JSON: json.loads raises RecursionError (P-20261116-13 rev 1b).
_NESTED_BODY = b"[" * 100000


class TestDeeplyNestedBody:

    async def test_no_model_running_gets_400(self, monkeypatch):
        async with _stack(
            monkeypatch, {"model-a": _make_app()}
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", data=_NESTED_BODY, headers=_JSON,
                )
                assert resp.status == 400
                data = await resp.json()
                assert "No model specified" in data["error"]["message"]
            assert stack.swapper._current is None

    @pytest.mark.parametrize("path", [
        pytest.param("/v1/chat/completions", id="model-less"),
        pytest.param("/v1/chat/completions?model=model-a", id="query-model"),
    ])
    async def test_running_model_gets_request(self, monkeypatch, path):
        seen: list[bytes] = []

        async def chat(request):
            seen.append(await request.read())
            return web.json_response({"choices": [], "id": "ok"})

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    path, data=_NESTED_BODY, headers=_JSON,
                )
                assert resp.status == 200
                assert (await resp.json())["id"] == "ok"
            assert seen == [_NESTED_BODY]
