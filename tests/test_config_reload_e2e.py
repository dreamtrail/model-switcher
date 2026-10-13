"""Real-process config reload (P-20261013-04).

Small real HTTP servers started from one shared script; the run goes through
the real config loader, port rewriting, process start/stop, health checks,
Swapper, proxy, and control API. Only the config file changes between steps.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
from src.control import CONTROL_HEADER
from src.swapper import Swapper
from tests.test_variants_e2e import SERVER


HEADERS = {CONTROL_HEADER: "1"}


def _hook(events: Path, name: str) -> str:
    code = f"open({str(events)!r}, 'a').write('hook {name}\\n')"
    return shlex.join([sys.executable, "-c", code])


async def test_reload_with_real_processes(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    events = tmp_path / "events"
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER)
    script = tmp_path / "run-model.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server_py))} "
        f"--port 8000 --events {shlex.quote(str(events))} \"$@\"\n"
    )
    script.chmod(0o755)
    config_path = tmp_path / "config.yaml"

    def write(data: dict) -> None:
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))

    write({
        "alpha": f"{script} --name alpha",
        "beta": f"{script} --name beta",
    })
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)
    swapper.config_path = config_path
    first_alpha = swapper.config.models["alpha"]
    first_alpha_copy = Path(first_alpha.cmd.split()[0])
    first_alpha_text = first_alpha_copy.read_text()
    beta = swapper.config.models["beta"]
    beta_spec = (beta.port, beta.cmd)

    app = web.Application()

    async def handler(request):
        return await swapper.handle_request(request)

    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()

    async def chat(client, model: str | None):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        if model is not None:
            body["model"] = model
        response = await client.post("/v1/chat/completions", json=body)
        return response.status, await response.json()

    async def reload(client):
        response = await client.post(
            "/api/v1/control/reload", headers=HEADERS
        )
        return response.status, await response.json()

    try:
        async with TestClient(server) as client:
            status, body = await chat(client, "alpha")
            assert (status, body["served_by"]) == (200, "alpha")
            assert body["port"] == first_alpha.port

            # alpha changed (new args + hook), beta unchanged, gamma added.
            write({
                "alpha": {
                    "cmd": f"{script} --name alpha2",
                    "shutdown_cmd": _hook(events, "alpha2"),
                },
                "beta": f"{script} --name beta",
                "gamma": f"{script} --name gamma",
            })
            status, data = await reload(client)
            assert status == 200, data
            assert data["reload"] == {
                "added": ["gamma"], "removed": [], "changed": ["alpha"],
                "running_changed": True,
            }
            assert data["active_model"] == "alpha"
            beta = swapper.config.models["beta"]
            assert (beta.port, beta.cmd) == beta_spec
            # The running copy is untouched; alpha's next start gets a new one.
            assert first_alpha_copy.read_text() == first_alpha_text
            new_alpha = swapper.config.models["alpha"]
            assert Path(new_alpha.cmd.split()[0]) != first_alpha_copy

            listed = await (await client.get("/v1/models")).json()
            assert [m["id"] for m in listed["data"]] == [
                "alpha", "beta", "gamma",
            ]

            # Running alpha keeps its old process and port.
            status, body = await chat(client, "alpha")
            assert (status, body["served_by"]) == (200, "alpha")
            assert body["port"] == first_alpha.port

            status, body = await chat(client, "gamma")
            assert (status, body["served_by"]) == (200, "gamma")

            # Next start of alpha uses the new command, port and hook.
            status, body = await chat(client, "alpha")
            assert (status, body["served_by"]) == (200, "alpha2")
            assert body["port"] == new_alpha.port != first_alpha.port

            # Remove running alpha: named requests 404, model-less still
            # reach the running process until a switch.
            write({
                "beta": f"{script} --name beta",
                "gamma": f"{script} --name gamma",
            })
            status, data = await reload(client)
            assert status == 200, data
            assert data["reload"] == {
                "added": [], "removed": ["alpha"], "changed": [],
                "running_changed": True,
            }
            status, body = await chat(client, "alpha")
            assert status == 404
            status, body = await chat(client, None)
            assert (status, body["served_by"]) == (200, "alpha2")

            # Invalid file: old config stays fully working.
            config_path.write_text("beta: [broken\n")
            status, data = await reload(client)
            assert status == 400
            assert data["error"]["code"] == "config_invalid"

            status, body = await chat(client, "beta")
            assert (status, body["served_by"]) == (200, "beta")
            assert body["port"] == beta_spec[0]
            listed = await (await client.get("/v1/models")).json()
            assert [m["id"] for m in listed["data"]] == ["beta", "gamma"]
    finally:
        await server.close()
        await swapper.shutdown()

    assert events.read_text().splitlines() == [
        "launch alpha",
        "stop alpha",
        "launch gamma",
        "stop gamma",
        "launch alpha2",
        "hook alpha2",
        "stop alpha2",
        "launch beta",
        "stop beta",
    ]


async def test_real_process_started_from_binary_given_by_path(
    tmp_path, monkeypatch
):
    """P-20261013-10: the interpreter's absolute path is a direct command.

    The model's command starts with ``sys.executable`` (a binary) and a
    stale ``--port 8000``. It must start without a temp copy, get the
    assigned port, serve requests, and survive a reload.
    """
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    events = tmp_path / "events"
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER)
    config_path = tmp_path / "config.yaml"

    def write(data: dict) -> None:
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))

    def entry(name: str) -> str:
        return (
            f"{sys.executable} {server_py} --port 8000 --name {name} "
            f"--events {events}"
        )

    write({"binary": entry("binary")})
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)
    swapper.config_path = config_path
    model = swapper.config.models["binary"]
    assert model.cmd == entry("binary").replace(
        "--port 8000", f"--port {model.port}"
    )
    assert list(swapper._temp_dir.iterdir()) == []

    app = web.Application()

    async def handler(request):
        return await swapper.handle_request(request)

    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "binary", "messages": []},
            )
            body = await response.json()
            assert (response.status, body["served_by"]) == (200, "binary")
            assert body["port"] == model.port

            write({"binary": entry("binary"), "other": entry("other")})
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 200, data
            assert data["reload"]["added"] == ["other"]
            assert data["reload"]["changed"] == []

            response = await client.post(
                "/v1/chat/completions",
                json={"model": "other", "messages": []},
            )
            body = await response.json()
            other = swapper.config.models["other"]
            assert (response.status, body["served_by"]) == (200, "other")
            assert body["port"] == other.port
            assert list(swapper._temp_dir.iterdir()) == []
    finally:
        await server.close()
        await swapper.shutdown()

    assert events.read_text().splitlines() == [
        "launch binary", "stop binary", "launch other", "stop other",
    ]
