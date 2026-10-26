"""Config-reload script-copy and binary-command tests (P-20261013-04).

Processes are faked here; tests/test_config_reload_e2e.py runs real ones.
Model/state tests live in tests/test_config_reload.py.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import sys

from aiohttp.test_utils import TestClient

from tests.config_reload_helpers import (
    cleanup,
    fakes,
    make_swapper,
    unreadable_script,
    write,
)
from tests.control_helpers import HEADERS
from tests.quick_switch_helpers import make_server


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
