"""Config reload without restart (P-20261013-04).

The swapper re-reads its YAML file on ``POST /api/v1/control/reload``.
Processes are faked here; tests/test_config_reload_e2e.py runs real ones.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sys

from aiohttp.test_utils import TestClient
import pytest
import yaml

import src.config as config_module
import src.swapper_lifecycle as lifecycle_module
from src.config import load_config
from src.control import CONTROL_HEADER
from src.process_manager import ProcessHandle
from src.swapper import Swapper
from tests.quick_switch_helpers import FakeProcess, make_server


HEADERS = {CONTROL_HEADER: "1"}


def write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class Fakes:
    """Fake process start/ready/stop; records what was started."""

    def __init__(self, monkeypatch):
        import src.swapper as module

        self.started: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.ready_gate: asyncio.Event | None = None
        self.stop_gate: asyncio.Event | None = None
        self.on_stop = None

        async def start(model, cmd, port):
            self.started.append((model, cmd, port))
            return ProcessHandle(model, port, FakeProcess(len(self.started)))

        async def ready(*args, **kwargs):
            if self.ready_gate is not None:
                await self.ready_gate.wait()

        async def stop(handle):
            if self.on_stop is not None:
                self.on_stop()
            if self.stop_gate is not None:
                await self.stop_gate.wait()
            self.stopped.append(handle.model_name)
            handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)


def make_swapper(path: Path) -> Swapper:
    swapper = Swapper(load_config(path), idle_timeout=0)
    swapper.config_path = path
    return swapper


def unreadable_script(path: Path) -> Path:
    """A text script the switcher cannot read (the copy step must fail)."""
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("file permissions do not stop this user from reading")
    return path


@pytest.fixture
def fakes(monkeypatch):
    return Fakes(monkeypatch)


@pytest.fixture
async def cleanup():
    swappers: list[Swapper] = []
    yield swappers
    for swapper in swappers:
        await swapper.shutdown()


class TestReloadModels:
    async def test_new_model_is_listed_and_loadable(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a --port 1"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        write(path, {"a": "serve-a --port 1", "b": "serve-b --port ${PORT}"})

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary == {
            "added": ["b"], "removed": [], "changed": [],
            "running_changed": False,
        }
        assert swapper.config.public_names() == ["a", "b"]
        port_b = swapper.config.models["b"].port
        assert swapper.config.models["b"].cmd == f"serve-b --port {port_b}"
        assert await swapper.control_load_model("b") == "success"
        assert fakes.started == [("b", f"serve-b --port {port_b}", port_b)]

    async def test_models_list_and_named_requests_follow_reload(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "gone": "serve-gone"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        server = await make_server(swapper)
        async with TestClient(server) as client:
            write(path, {"a": "serve-a", "new": "serve-new"})
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 200, data
            assert response.headers["Cache-Control"] == "no-store"
            assert data["reload"] == {
                "added": ["new"], "removed": ["gone"], "changed": [],
                "running_changed": False,
            }
            assert data["models"] == ["a", "new"]
            assert set(data) == set(swapper.control_state()) | {"reload"}

            listed = await (await client.get("/v1/models")).json()
            assert [item["id"] for item in listed["data"]] == ["a", "new"]
            gone = await client.post(
                "/v1/chat/completions", json={"model": "gone"}
            )
            assert gone.status == 404
            assert fakes.started == []

    async def test_unchanged_entries_keep_their_model_config(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {
            "a": "serve-a --port 1",
            "b": {"cmd": "serve-b", "shutdown_cmd": "true hook-b"},
            "c": "serve-c",
        })
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = dict(swapper.config.models)
        old_c = (old["c"].port, old["c"].cmd)
        write(path, {
            "a": "serve-a --port 1",
            "b": {"cmd": "serve-b", "shutdown_cmd": "true hook-b"},
            "c": "serve-c --ctx 8",
        })

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == ["c"]
        assert swapper.config.models["a"] is old["a"]
        assert swapper.config.models["b"] is old["b"]
        assert swapper.config.models["c"] is not old["c"]
        # The old object is not mutated (a running process may use it).
        assert (old["c"].port, old["c"].cmd) == old_c
        new_c = swapper.config.models["c"]
        assert new_c.cmd == f"serve-c --ctx 8 --port {new_c.port}"

    @pytest.mark.parametrize(
        "change",
        [
            {"cmd": "serve-a --ctx 8"},
            {"cmd": "serve-a", "shutdown_cmd": "hook-new"},
            {"cmd": "serve-a", "shutdown_cmd": "hook", "shutdown_timeout": 9},
        ],
    )
    async def test_cmd_hook_and_hook_timeout_changes_count_as_changed(
        self, tmp_path, fakes, cleanup, change
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": {"cmd": "serve-a", "shutdown_cmd": "hook"}})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        write(path, {"a": change})

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == ["a"]

    async def test_new_ports_avoid_the_running_process_and_kept_entries(
        self, tmp_path, fakes, cleanup, monkeypatch
    ):
        # Fixed, distinct ports for the running model "a" and the kept
        # model "b", so the test cannot pass by luck if one reservation
        # is missing.
        running_port, kept_port, clear = 41000, 42000, 43000
        startup = iter([running_port, kept_port])
        # The reload's candidates: the running port, the kept port,
        # then a clear one. Only actual reserved bases must be skipped.
        candidates = iter([running_port, kept_port, clear])
        real_find_free_port = config_module._find_free_port
        picking = False
        picks: list[set[int]] = []

        def fake_os_port():
            # Fake OS candidates only inside the reload's port picker, so
            # the loader's own port calls cannot use them up.
            if picking:
                return next(candidates)
            return next(startup, None) or real_find_free_port()

        def spy_find_free_port(reserved=frozenset()):
            nonlocal picking
            picks.append(set(reserved))
            picking = True
            try:
                return config_module.find_free_port(reserved)
            finally:
                picking = False

        monkeypatch.setattr(config_module, "_find_free_port", fake_os_port)
        monkeypatch.setattr(
            lifecycle_module, "find_free_port", spy_find_free_port
        )
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert swapper.config.models["a"].port == running_port
        assert swapper.config.models["b"].port == kept_port
        assert await swapper.control_load_model("a") == "success"
        assert swapper._current.port == running_port
        write(path, {"a": "serve-a --ctx 8", "b": "serve-b"})

        kind, _ = swapper.control_reload_config()

        assert kind == "success"
        assert swapper.config.models["a"].port == clear
        assert picks == [{running_port, kept_port}]
        assert swapper.config.models["b"].port == kept_port


class TestRunningModel:
    async def test_changed_running_model_keeps_running_then_uses_new_spec(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        handle = swapper._current
        old_port = handle.port
        write(path, {
            "a": {"cmd": "serve-a --ctx 8", "shutdown_cmd": "true hook-a"},
            "b": "serve-b",
        })

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["running_changed"] is True
        assert swapper._current is handle
        assert swapper.active_model == "a"
        assert handle.port == old_port
        assert handle.shutdown_cmd is None
        # Loading the running model again is a no-op (old settings stay).
        assert await swapper.control_load_model("a") == "success"
        assert len(fakes.started) == 1

        assert await swapper.control_unload_model() == "success"
        assert await swapper.control_load_model("a") == "success"
        new = swapper.config.models["a"]
        assert new.port != old_port
        assert fakes.started[-1] == ("a", new.cmd, new.port)
        assert new.cmd == f"serve-a --ctx 8 --port {new.port}"
        assert swapper._current.shutdown_cmd == "true hook-a"

    async def test_removed_running_model_keeps_serving_until_switch(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        handle = swapper._current
        write(path, {"b": "serve-b"})

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary == {
            "added": [], "removed": ["a"], "changed": [],
            "running_changed": True,
        }
        state = swapper.control_state()
        assert state["models"] == ["b"]
        assert state["active_model"] == "a"
        assert state["lifecycle"] == "ready"
        assert swapper._current is handle
        assert await swapper.control_load_model("a") == "unknown_model"

        assert await swapper.control_load_model("b") == "success"
        assert fakes.stopped == ["a"]
        assert swapper.active_model == "b"

    async def test_removed_running_model_can_be_unloaded(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        write(path, {"b": "serve-b"})
        assert swapper.control_reload_config()[0] == "success"

        assert await swapper.control_unload_model() == "success"

        assert fakes.stopped == ["a"]
        assert swapper.control_state()["lifecycle"] == "idle"

    async def test_start_uses_entry_resolved_before_previous_model_stops(
        self, tmp_path, fakes, cleanup
    ):
        """Defensive fix: no config lookup after the stop await."""
        path = tmp_path / "config.yaml"
        write(path, {
            "a": "serve-a",
            "b": {"cmd": "serve-b", "shutdown_cmd": "true hook-b"},
        })
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        resolved_b = swapper.config.models["b"]
        replacement = load_config(path)
        replacement.models.pop("b")

        def swap_config():
            swapper.config = replacement

        fakes.on_stop = swap_config

        assert await swapper.control_load_model("b") == "success"

        assert fakes.started[-1] == ("b", resolved_b.cmd, resolved_b.port)
        assert swapper._current.shutdown_cmd == "true hook-b"


class TestRefusals:
    async def test_invalid_or_missing_config_keeps_old_config(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config
        old_a = (old.models["a"].cmd, old.models["a"].port)
        server = await make_server(swapper)
        async with TestClient(server) as client:
            cases = [
                ("a: [unclosed\n", "Invalid YAML in config.yaml: "),
                ("- just\n- a list\n", "Config must be a YAML mapping"),
                ("{}\n", "Config must define at least one model"),
                ("a: ''\n", "Model 'a' has an empty command"),
                ("a: {cmd: 123}\n", "Model 'a' 'cmd' must be a string"),
                (
                    "q:\n  variants:\n    t: {cmd: [x, y]}\n",
                    "Model 'q' variant 't' 'cmd' must be a string, got list",
                ),
                ("a: {cmd: x, shutdown_cmd: 5}\n", "invalid shutdown_cmd"),
                ("123: serve-x\n", "Model name 123 (int) must be a string; put quotes"),
                ("yes: serve-x\n", "Model name True (bool) must be a string; put quotes"),
                ("~: serve-x\n", "Model name null must be a string; put quotes"),
                (
                    "q:\n  variants:\n    1: serve-x\n",
                    "Model 'q' variant name 1 (int) must be a string; put quotes",
                ),
                ("q: {cmd: x, default: t}\n", "has 'default' but no 'variants'"),
                (None, "Config not reloaded: Config file not found: config.yaml"),
            ]
            for text, expected in cases:
                if text is None:
                    path.unlink()
                else:
                    path.write_text(text)
                response = await client.post(
                    "/api/v1/control/reload", headers=HEADERS
                )
                data = await response.json()
                assert response.status == 400, data
                error = data["error"]
                assert error["code"] == "config_invalid"
                assert error["message"].startswith("Config not reloaded: ")
                assert expected in error["message"]
                assert "\n" not in error["message"]
                assert len(error["message"]) <= 330
                assert swapper.config is old
                assert (old.models["a"].cmd, old.models["a"].port) == old_a

            load = await client.put(
                "/api/v1/control/model", json={"model": "a"}, headers=HEADERS
            )
            assert load.status == 200
        assert fakes.started == [("a", old_a[0], old_a[1])]

    async def test_error_messages_hide_paths_and_file_contents(
        self, tmp_path, fakes, cleanup
    ):
        """Reload errors show only the file's base name, no source text."""
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        secret = "serve-secret --api-key TOPSECRET"
        cases = {
            "a: " + secret + "\nb: [broken\n": (
                "Invalid YAML in config.yaml: expected ',' or ']', but got "
                "'<stream end>' (line 3, column 1)"
            ),
            "a: " + secret + "\n  b: c: d\n": (
                "Invalid YAML in config.yaml: mapping values are not "
                "allowed here (line 2, column 4)"
            ),
        }
        for text, expected in cases.items():
            path.write_text(text)
            kind, message = swapper.control_reload_config()
            assert (kind, message) == ("config_invalid", expected)
            assert "TOPSECRET" not in message
            assert str(tmp_path) not in message
        path.unlink()
        kind, message = swapper.control_reload_config()
        assert message == "Config file not found: config.yaml"
        path.mkdir()
        kind, message = swapper.control_reload_config()
        assert kind == "config_invalid"
        assert message == "Is a directory: config.yaml"
        path.rmdir()
        path.write_text(
            "a: serve-a\n" + "x" * 400 + ": ''\n"
        )
        kind, message = swapper.control_reload_config()
        assert kind == "config_invalid"
        assert len(message) <= 300 and message.endswith("...")

    async def test_unreadable_script_keeps_old_config(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config
        script = unreadable_script(tmp_path / "bad.sh")
        write(path, {"a": "serve-a", "b": f"{script}"})

        kind, message = swapper.control_reload_config()

        assert kind == "config_invalid"
        assert "Permission denied" in message
        assert swapper.config is old

    async def test_reload_without_known_path_is_config_invalid(
        self, fakes, cleanup, tmp_path
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = Swapper(load_config(path), idle_timeout=0)
        cleanup.append(swapper)

        assert swapper.control_reload_config() == (
            "config_invalid", "Config file path is not known"
        )

    async def test_reload_during_in_progress_load_is_control_busy(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config
        fakes.ready_gate = asyncio.Event()
        server = await make_server(swapper)
        async with TestClient(server) as client:
            load = asyncio.create_task(swapper.control_load_model("a"))
            while swapper.control_state()["lifecycle"] != "loading":
                await asyncio.sleep(0)
            write(path, {"b": "serve-b", "c": "serve-c"})
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 409
            assert data["error"]["code"] == "control_busy"
            assert swapper.config is old

            fakes.ready_gate.set()
            assert await load == "success"
            # Once the load is done, the same file reloads fine.
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 200, data
            assert data["reload"]["removed"] == ["a"]
            assert data["reload"]["running_changed"] is True

    async def test_reload_during_unload_is_control_busy(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        fakes.stop_gate = asyncio.Event()
        unload = asyncio.create_task(swapper.control_unload_model())
        while swapper.control_state()["lifecycle"] != "unloading":
            await asyncio.sleep(0)

        assert swapper.control_reload_config() == ("control_busy", None)

        fakes.stop_gate.set()
        assert await unload == "success"
        assert swapper.control_reload_config()[0] == "success"

    async def test_reload_during_idle_unload_is_control_busy(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = Swapper(load_config(path), idle_timeout=0.02)
        swapper.config_path = path
        cleanup.append(swapper)
        old = swapper.config
        assert await swapper.control_load_model("a") == "success"
        fakes.stop_gate = asyncio.Event()
        server = await make_server(swapper)
        async with TestClient(server) as client:
            for _ in range(500):
                if swapper.control_state()["lifecycle"] == "unloading":
                    break
                await asyncio.sleep(0.01)
            assert swapper.control_state()["lifecycle"] == "unloading"
            assert swapper._stop_only_operation == "idle"
            write(path, {"a": "serve-a", "b": "serve-b"})

            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 409
            assert data["error"]["code"] == "control_busy"
            assert swapper.config is old

            fakes.stop_gate.set()
            for _ in range(500):
                if swapper.control_state()["lifecycle"] == "idle":
                    break
                await asyncio.sleep(0.01)
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            assert response.status == 200
            assert (await response.json())["reload"]["added"] == ["b"]

    async def test_reload_after_shutdown_starts_is_refused(
        self, tmp_path, fakes
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        server = await make_server(swapper)
        async with TestClient(server) as client:
            await swapper.shutdown()
            write(path, {"a": "serve-a", "b": "serve-b"})
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
        assert response.status == 503
        assert data["error"]["code"] == "shutting_down"
        assert swapper.config.public_names() == ["a"]

    async def test_reload_body_is_rejected(self, tmp_path, fakes, cleanup):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config
        server = await make_server(swapper)
        async with TestClient(server) as client:
            response = await client.post(
                "/api/v1/control/reload", data=b"x", headers=HEADERS
            )
            data = await response.json()
        assert response.status == 400
        assert data["error"]["code"] == "invalid_control_request"
        assert swapper.config is old


class TestVariantsAndSettings:
    async def test_chosen_variant_survives_reload_unless_removed(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        groups = {
            "q": {"variants": {"text": "serve-t", "vision": "serve-v"}},
            "r": {"variants": {"x": "serve-x", "y": "serve-y"}},
        }
        write(path, groups)
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.config.variants["q"].active = "vision"
        swapper.config.variants["r"].active = "y"
        write(path, {
            "q": {"variants": {"text": "serve-t", "vision": "serve-v2"}},
            "r": {"default": "x", "variants": {"x": "serve-x", "z": "serve-z"}},
        })

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary == {
            "added": ["r@z"], "removed": ["r@y"], "changed": ["q@vision"],
            "running_changed": False,
        }
        assert swapper.config.variants["q"].active == "vision"
        assert swapper.config.variants["r"].active == "x"
        assert swapper.config.resolve("q") == "q@vision"

    async def test_runtime_settings_are_not_changed(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, {"a": "serve-a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert swapper.control_apply_settings(
            automatic_switch=False, quick_switch=True,
            idle_timeout_seconds=42,
        ) == "success"
        before = swapper.control_state()["settings"]
        write(path, {"a": "serve-a", "b": "serve-b"})

        assert swapper.control_reload_config()[0] == "success"

        assert swapper.control_state()["settings"] == before


SCRIPT = "#!/bin/sh\nexec {python} -c pass --port 8000 \"$@\"\n"


class TestTempScripts:
    async def test_unchanged_script_keeps_copy_changed_gets_new_copy(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        script.chmod(0o755)
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --name a", "b": f"{script} --name b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        copy_a = Path(swapper.config.models["a"].cmd.split()[0])
        copy_b = Path(swapper.config.models["b"].cmd.split()[0])
        content_b = copy_b.read_text()
        assert await swapper.control_load_model("b") == "success"
        write(path, {"a": f"{script} --name a", "b": f"{script} --name b2"})

        assert swapper.control_reload_config()[0] == "success"
        # A second reload of a changed entry must make yet another copy.
        write(path, {"a": f"{script} --name a", "b": f"{script} --name b3"})
        assert swapper.control_reload_config()[0] == "success"

        assert Path(swapper.config.models["a"].cmd.split()[0]) == copy_a
        new_b = Path(swapper.config.models["b"].cmd.split()[0])
        assert new_b != copy_b
        assert new_b.parent == copy_b.parent
        assert "-r2-" in new_b.name
        # The copy the running process came from is untouched.
        assert copy_b.read_text() == content_b
        port_b = swapper.config.models["b"].port
        assert f"--port {port_b}" in new_b.read_text()
        assert len(list(copy_b.parent.iterdir())) == 4

    async def test_failed_reloads_do_not_advance_the_reload_counter(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --name a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert swapper._config_generation == 0

        # Invalid file, then a script that cannot be copied.
        path.write_text("a: [unclosed\n")
        assert swapper.control_reload_config()[0] == "config_invalid"
        bad = unreadable_script(tmp_path / "bad.sh")
        write(path, {"a": f"{script} --name a", "b": f"{bad}"})
        assert swapper.control_reload_config()[0] == "config_invalid"
        # Refused reloads do not count either.
        swapper._swapping = True
        assert swapper.control_reload_config() == ("control_busy", None)
        swapper._swapping = False
        assert swapper._config_generation == 0

        write(path, {"a": f"{script} --name a2"})
        assert swapper.control_reload_config()[0] == "success"
        assert swapper._config_generation == 1
        copy = Path(swapper.config.models["a"].cmd.split()[0])
        assert "-r1-" in copy.name

    async def test_edited_script_with_same_config_line_counts_as_changed(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        script.chmod(0o755)
        path = tmp_path / "config.yaml"
        write(path, {
            "a": f"{script} --name a",
            "b": f"{script} --name b",
            "c": "serve-c --port 1",
        })
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = dict(swapper.config.models)
        old_copy = Path(old["a"].cmd.split()[0])
        old_text = old_copy.read_text()
        assert await swapper.control_load_model("a") == "success"
        # Same config text; only the script file changes.
        script.write_text(
            SCRIPT.format(python=shlex.quote(sys.executable))
            + "# edited\n"
        )

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary == {
            "added": [], "removed": [], "changed": ["a", "b"],
            "running_changed": True,
        }
        assert swapper.config.models["c"] is old["c"]
        new = swapper.config.models["a"]
        assert new is not old["a"]
        new_copy = Path(new.cmd.split()[0])
        assert new_copy != old_copy
        assert "# edited" in new_copy.read_text()
        assert f"--port {new.port}" in new_copy.read_text()
        # The running process keeps its old copy untouched.
        assert old_copy.read_text() == old_text
        assert await swapper.control_unload_model() == "success"
        assert await swapper.control_load_model("a") == "success"
        assert fakes.started[-1] == ("a", new.cmd, new.port)

    async def test_unedited_script_stays_unchanged_across_reloads(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --name a", "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = dict(swapper.config.models)
        # A file touched without changing its bytes is still unchanged.
        script.write_text(script.read_text())

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == []
        assert swapper.config.models["a"] is old["a"]
        assert swapper.config.models["b"] is old["b"]

    async def test_script_edit_to_unreadable_content_keeps_old_config(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --name a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config
        unreadable_script(script)

        kind, message = swapper.control_reload_config()

        assert kind == "config_invalid"
        assert "Permission denied" in message
        assert swapper.config is old
        assert swapper._config_generation == 0

    async def test_script_replaced_by_binary_becomes_direct_command(
        self, tmp_path, fakes, cleanup
    ):
        script = tmp_path / "run.sh"
        script.write_text(SCRIPT.format(python=shlex.quote(sys.executable)))
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --name a"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert swapper.config.models["a"].spec[3] is not None
        script.write_bytes(b"\x7fELF\x00 binary")

        kind, summary = swapper.control_reload_config()

        assert kind == "success", summary
        assert summary["changed"] == ["a"]
        new = swapper.config.models["a"]
        assert new.cmd == f"{script} --name a --port {new.port}"
        assert new.spec[3] is None

    async def test_changed_port_placeholder_script_runs_a_temp_copy_after_reload(
        self, tmp_path, fakes, cleanup
    ):
        """Reload gives a ${PORT} script entry the same result as startup.

        The loader fills in ${PORT} before the script copy, so the entry
        runs a temp copy with the new port in its args and its body.
        """
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\n# port ${PORT}\nexit 0\n")
        script.chmod(0o755)
        path = tmp_path / "config.yaml"
        write(path, {"a": f"{script} --port ${{PORT}}", "b": "serve-b --port ${PORT}"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        startup = swapper.config.models["a"]
        startup_copy = Path(startup.cmd.split()[0])
        assert startup_copy != script and startup_copy.parent == swapper._temp_dir
        assert f"# port {startup.port}" in startup_copy.read_text()
        assert startup.cmd.endswith(f"--port {startup.port}")
        old_b = swapper.config.models["b"]
        # The script changes; the config text stays the same.
        script.write_text("#!/bin/sh\n# port ${PORT}\n# edited\nexit 0\n")

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == ["a"]
        new = swapper.config.models["a"]
        copy = Path(new.cmd.split()[0])
        assert copy != script and copy != startup_copy
        assert copy.parent == swapper._temp_dir and "-r1-" in copy.name
        assert new.port != startup.port
        assert new.cmd == f"{copy} --port {new.port}"
        text = copy.read_text()
        assert f"# port {new.port}" in text and "# edited" in text
        assert "${PORT}" not in text
        # The old copy is untouched, and the placeholder command that is
        # not a script keeps its raw form (the server gets the port as is).
        assert f"# port {startup.port}" in startup_copy.read_text()
        assert swapper.config.models["b"] is old_b
        assert await swapper.control_load_model("a") == "success"
        assert fakes.started[-1] == ("a", new.cmd, new.port)


class TestBinaryGivenByPath:
    """A program given by absolute path is a direct command (P-20261013-10)."""

    @staticmethod
    def _nul_binary(path: Path) -> Path:
        path.write_bytes(b"\x7fELF\x00\x01 not text \xff\xfe")
        path.chmod(0o755)
        return path

    async def test_startup_runs_binary_directly(
        self, tmp_path, fakes, cleanup
    ):
        binary = self._nul_binary(tmp_path / "server-bin")
        config_path = tmp_path / "config.yaml"
        write(config_path, {
            "nul": f"{binary} --model m --port 8000",
            "exe": f"{sys.executable} -m srv --port 8000",
            "bare": f"{binary}",
        })
        swapper = make_swapper(config_path)
        cleanup.append(swapper)

        for name, tail in (
            ("nul", "--model m"), ("exe", "-m srv"), ("bare", ""),
        ):
            mc = swapper.config.models[name]
            exe = sys.executable if name == "exe" else binary
            assert mc.cmd == f"{exe} {tail} --port {mc.port}".replace(
                "  ", " "
            )
            assert mc.spec[3] is None
        assert list(swapper._temp_dir.iterdir()) == []
        assert await swapper.control_load_model("nul") == "success"
        nul = swapper.config.models["nul"]
        assert fakes.started == [("nul", nul.cmd, nul.port)]

    async def test_reload_with_binary_entries_succeeds(
        self, tmp_path, fakes, cleanup
    ):
        binary = self._nul_binary(tmp_path / "server-bin")
        config_path = tmp_path / "config.yaml"
        write(config_path, {"a": f"{binary} --port 8000"})
        swapper = make_swapper(config_path)
        cleanup.append(swapper)
        write(config_path, {
            "a": f"{binary} --port 8000",
            "b": f"{binary} --port 8000 --name b",
            "c": f"{sys.executable} -m srv --port ${{PORT}}",
        })

        kind, summary = swapper.control_reload_config()

        assert kind == "success", summary
        assert summary["added"] == ["b", "c"] and summary["changed"] == []
        b = swapper.config.models["b"]
        assert b.cmd == f"{binary} --port {b.port} --name b"
        c = swapper.config.models["c"]
        assert c.cmd == f"{sys.executable} -m srv --port {c.port}"
        assert list(swapper._temp_dir.iterdir()) == []

        # Editing the binary does not make the entry changed (not a script).
        binary.write_bytes(b"\x00other")
        kind, summary = swapper.control_reload_config()
        assert kind == "success"
        assert summary["changed"] == []

    async def test_reload_over_http_with_binary_entry(
        self, tmp_path, fakes, cleanup
    ):
        binary = self._nul_binary(tmp_path / "server-bin")
        config_path = tmp_path / "config.yaml"
        write(config_path, {"a": "serve-a"})
        swapper = make_swapper(config_path)
        cleanup.append(swapper)
        server = await make_server(swapper)
        async with TestClient(server) as client:
            write(config_path, {"a": "serve-a", "b": f"{binary} --port 1"})
            response = await client.post(
                "/api/v1/control/reload", headers=HEADERS
            )
            data = await response.json()
            assert response.status == 200, data
            assert data["reload"]["added"] == ["b"]
