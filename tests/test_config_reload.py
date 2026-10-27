"""Config-reload model/state tests (P-20261013-04).

The swapper re-reads its YAML file on ``POST /api/v1/control/reload``.
Processes are faked here; tests/test_config_reload_e2e.py runs real ones.
Script/binary tests live in tests/test_config_reload_scripts.py.
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient
import pytest

import src.config as config_module
import src.swapper_lifecycle as lifecycle_module
from src.config import load_config
from src.swapper import Swapper
from tests.config_reload_helpers import (
    cleanup,
    fakes,
    make_swapper,
    unreadable_script,
    write,
)
from tests.control_helpers import HEADERS
from tests.quick_switch_helpers import make_server


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

    @pytest.mark.parametrize(
        ("before", "after", "changed"),
        [
            ({"cmd": "serve-a"}, {"cmd": "serve-a", "stop_grace": 20}, True),
            ({"cmd": "serve-a", "stop_grace": 20},
             {"cmd": "serve-a", "stop_grace": 30}, True),
            ({"cmd": "serve-a", "stop_grace": 20}, {"cmd": "serve-a"}, True),
            ({"cmd": "serve-a", "stop_grace": 20},
             {"cmd": "serve-a", "stop_grace": 20}, False),
        ],
    )
    async def test_stop_grace_changes_count_as_changed(
        self, tmp_path, fakes, cleanup, before, after, changed
    ):
        """P-20261027-01: stop_grace is part of the launch spec."""
        path = tmp_path / "config.yaml"
        write(path, {"a": before})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        old = swapper.config.models["a"]
        write(path, {"a": after})

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == (["a"] if changed else [])
        assert (swapper.config.models["a"] is old) is not changed
        assert swapper.config.models["a"].stop_grace == after.get("stop_grace")

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

    async def test_running_model_keeps_its_stop_grace_until_restarted(
        self, tmp_path, fakes, cleanup
    ):
        """P-20261027-01: the handle holds the stop_grace it started with."""
        path = tmp_path / "config.yaml"
        write(path, {"a": {"cmd": "serve-a", "stop_grace": 20}, "b": "serve-b"})
        swapper = make_swapper(path)
        cleanup.append(swapper)
        assert await swapper.control_load_model("a") == "success"
        handle = swapper._current
        assert handle.stop_grace == 20
        write(path, {"a": {"cmd": "serve-a", "stop_grace": 45}, "b": "serve-b"})

        kind, summary = swapper.control_reload_config()

        assert kind == "success"
        assert summary["changed"] == ["a"]
        assert summary["running_changed"] is True
        assert swapper._current is handle and handle.stop_grace == 20

        assert await swapper.control_unload_model() == "success"
        assert await swapper.control_load_model("a") == "success"
        assert swapper._current.stop_grace == 45
        assert await swapper.control_load_model("b") == "success"
        assert swapper._current.stop_grace is None


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
