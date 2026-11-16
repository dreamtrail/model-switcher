"""Tests for src/port_rewrite.py — port rewriting for commands and scripts.

Derived from plan P-20260612-07 acceptance criteria:
- Script files: copied to temp, port replaced in content, made executable
- Direct commands: port replaced in command string
- No port flag: --port <N> appended
- ${PORT} still works
- Regex edge cases (lookbehind guard, --listen-port, --port=)
- Temp dir create/cleanup
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from src.port_rewrite import (
    _PORT_FLAGS_RE,
    _TEXT_CHUNK,
    cleanup_temp_dir,
    create_temp_dir,
    is_script_command,
    rewrite_command,
    script_fingerprint,
)


class TestRewriteCommandPortPlaceholder:

    def test_dollar_port_substituted(self):
        """${PORT} is replaced with the given port."""
        cmd, temp = rewrite_command("cmd --port ${PORT}", 9999)
        assert cmd == "cmd --port 9999"
        assert temp is None

    def test_dollar_port_in_middle(self):
        """${PORT} in middle of command is replaced."""
        cmd, temp = rewrite_command("cmd --listen ${PORT} --model foo", 8080)
        assert cmd == "cmd --listen 8080 --model foo"
        assert temp is None

    def test_dollar_port_no_temp_file(self):
        """${PORT} substitution never creates temp files."""
        _, temp = rewrite_command("cmd --port ${PORT}", 1234)
        assert temp is None


class TestRewriteCommandDirect:

    def test_port_flag_replaced(self):
        """--port 8000 is replaced with --port <N>."""
        cmd, temp = rewrite_command("cmd --port 8000", 9999)
        assert cmd == "cmd --port 9999"
        assert temp is None

    def test_port_eq_flag_replaced(self):
        """--port=8000 is replaced with --port=<N>."""
        cmd, temp = rewrite_command("cmd --port=8000", 9999)
        assert cmd == "cmd --port=9999"
        assert temp is None

    def test_listen_port_flag_replaced(self):
        """--listen-port 8000 is replaced with --listen-port <N>."""
        cmd, temp = rewrite_command("cmd --listen-port 8000", 9999)
        assert cmd == "cmd --listen-port 9999"
        assert temp is None

    def test_port_double_quoted_replaced(self):
        """--port \"8000\" is replaced (vLLM style)."""
        cmd, temp = rewrite_command('cmd --port "8000"', 9999)
        assert cmd == 'cmd --port "9999"'
        assert temp is None

    def test_port_single_quoted_replaced(self):
        """--port '8000' is replaced."""
        cmd, temp = rewrite_command("cmd --port '8000'", 9999)
        assert cmd == "cmd --port '9999'"
        assert temp is None

    def test_no_port_flag_appended(self):
        """No port flag: --port <N> is appended."""
        cmd, temp = rewrite_command("cmd --model foo", 9999)
        assert cmd == "cmd --model foo --port 9999"
        assert temp is None

    def test_multiple_port_flags_all_replaced(self):
        """Multiple --port flags: all matches are replaced."""
        cmd, temp = rewrite_command("cmd --port 1111 --port 2222", 9999)
        assert cmd == "cmd --port 9999 --port 9999"
        assert temp is None

    def test_flag_already_at_port_not_appended(self):
        """--port N already equal to the port: kept once, not appended."""
        for cmd in ("cmd --port 9999 --x", "cmd --port=9999"):
            rewritten, temp = rewrite_command(cmd, 9999)
            assert rewritten == cmd
            assert temp is None


class TestRewriteCommandScriptFile:

    def test_script_copied_to_temp(self, tmp_path: Path):
        """Script file is copied to temp dir with port replaced."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --port 8000\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(f"{script}", 9999, temp_dir=temp_dir)
            assert temp is not None
            assert temp.exists()
            content = temp.read_text()
            assert "--port 9999" in content
            assert "--port 8000" not in content
        finally:
            cleanup_temp_dir(temp_dir)

    def test_script_with_args_preserved(self, tmp_path: Path):
        """Script with args: args preserved, port replaced."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --port 8000 --model foo\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(f"{script} --ctx 4096", 9999, temp_dir=temp_dir)
            assert temp is not None
            assert "--ctx 4096" in cmd
        finally:
            cleanup_temp_dir(temp_dir)

    def test_script_without_port_flag_appended(self, tmp_path: Path):
        """Script with no port flag: --port appended to command."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --model foo\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(f"{script}", 9999, temp_dir=temp_dir)
            assert temp is not None
            assert "--port 9999" in cmd
        finally:
            cleanup_temp_dir(temp_dir)

    def test_script_executable_permission_preserved(self, tmp_path: Path):
        """Temp script copy preserves executable permission."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\necho hello\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            _, temp = rewrite_command(f"{script}", 9999, temp_dir=temp_dir)
            assert temp is not None
            mode = temp.stat().st_mode
            assert mode & stat.S_IXUSR  # user execute bit set
        finally:
            cleanup_temp_dir(temp_dir)

    def test_script_without_temp_dir_appends_port(self, tmp_path: Path):
        """Script without temp_dir: --port appended, no temp file."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --port 8000\n")
        script.chmod(0o755)

        cmd, temp = rewrite_command(f"{script}", 9999, temp_dir=None)
        assert temp is None
        assert "--port 9999" in cmd

    def test_script_with_dollar_port_in_content(self, tmp_path: Path):
        """${PORT} in script content is substituted."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --port ${PORT} --model foo\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(f"{script}", 9999, temp_dir=temp_dir)
            assert temp is not None
            content = temp.read_text()
            assert "${PORT}" not in content
            assert "--port 9999" in content
        finally:
            cleanup_temp_dir(temp_dir)

    def test_script_args_with_port_flag_rewritten(self, tmp_path: Path):
        """Port flags in script args are rewritten."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --model foo\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(
                f"{script} --port 8000 --model bar", 9999, temp_dir=temp_dir
            )
            assert temp is not None
            assert "--port 8000" not in cmd
            assert "--port 9999" in cmd
            assert "--model bar" in cmd
        finally:
            cleanup_temp_dir(temp_dir)

    def test_dollar_port_in_args_uses_fast_path(self, tmp_path: Path):
        """${PORT} in args triggers fast path (no script rewrite)."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --model foo\n")
        script.chmod(0o755)

        # ${PORT} in command string takes fast path, bypasses script rewriting
        cmd, temp = rewrite_command(
            f"{script} --port ${{PORT}}", 9999, temp_dir=create_temp_dir()
        )
        assert temp is None  # fast path, no temp file
        assert "${PORT}" not in cmd
        assert "--port 9999" in cmd


class TestBinaryGivenByPath:
    """A non-text file given by path is a direct command (P-20261013-10)."""

    @pytest.fixture(params=["nul", "invalid-utf8", "late-invalid"])
    def binary(self, request, tmp_path: Path) -> Path:
        path = tmp_path / "server-bin"
        if request.param == "nul":
            path.write_bytes(b"\x7fELF\x00\x01\x02 --port 1")
        elif request.param == "invalid-utf8":
            path.write_bytes(b"\xff\xfe not utf-8")
        else:
            # No NUL, valid text for the first chunk, bad bytes after it.
            path.write_bytes(b"#!/bin/sh\n" + b"x" * 200_000 + b"\xff\xfe")
        path.chmod(0o755)
        return path

    def test_flag_replaced_without_temp_copy(self, binary: Path, tmp_path):
        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(
                f"{binary} --model m --port 8000", 9999, temp_dir=temp_dir
            )
            assert (cmd, temp) == (f"{binary} --model m --port 9999", None)
            assert list(temp_dir.iterdir()) == []
        finally:
            cleanup_temp_dir(temp_dir)

    def test_port_appended_when_no_flag(self, binary: Path):
        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(f"{binary}", 9999, temp_dir=temp_dir)
            assert (cmd, temp) == (f"{binary} --port 9999", None)
        finally:
            cleanup_temp_dir(temp_dir)

    def test_not_script_command_and_no_fingerprint(self, binary: Path):
        assert not is_script_command(f"{binary} --port 1")
        assert script_fingerprint(f"{binary} --port 1") is None

    def test_interpreter_by_absolute_path(self):
        temp_dir = create_temp_dir()
        try:
            cmd, temp = rewrite_command(
                f"{sys.executable} -m srv --port 8000", 9999,
                temp_dir=temp_dir,
            )
            assert (cmd, temp) == (
                f"{sys.executable} -m srv --port 9999", None,
            )
            assert not is_script_command(f"{sys.executable} -m srv")
        finally:
            cleanup_temp_dir(temp_dir)

    def test_text_file_is_still_a_script(self, tmp_path: Path):
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\necho h\u00e9llo --port 8000\n")
        assert is_script_command(f"{script} --x")
        assert script_fingerprint(f"{script}") is not None

    @pytest.mark.parametrize(
        "char, head_bytes",
        [
            ("\u20ac", 1),      # 3-byte char, split 1 + 2
            ("\u20ac", 2),      # 3-byte char, split 2 + 1
            ("\U0001F600", 1),  # 4-byte char, split 1 + 3
            ("\U0001F600", 2),  # 4-byte char, split 2 + 2
            ("\U0001F600", 3),  # 4-byte char, split 3 + 1
        ],
    )
    def test_multibyte_char_across_chunk_boundary_is_text(
        self, tmp_path: Path, char: str, head_bytes: int
    ):
        encoded = char.encode("utf-8")
        data = b"#" * (_TEXT_CHUNK - head_bytes) + encoded + b"\n"
        # The character straddles the end of the first chunk.
        assert data[:_TEXT_CHUNK].endswith(encoded[:head_bytes])
        script = tmp_path / "wide.sh"
        script.write_bytes(data)
        assert is_script_command(f"{script}")

    def test_truncated_multibyte_char_at_eof_is_a_program(
        self, tmp_path: Path
    ):
        short = tmp_path / "short.bin"
        short.write_bytes(b"#!/bin/sh\n\xe2\x82")  # 3-byte char cut off
        assert not is_script_command(f"{short}")
        # Same cut exactly at the end of a full first chunk.
        edge = tmp_path / "edge.bin"
        edge.write_bytes(b"#" * (_TEXT_CHUNK - 1) + "\U0001F600".encode()[:3])
        assert not is_script_command(f"{edge}")

    def test_empty_file_is_a_script(self, tmp_path: Path):
        empty = tmp_path / "empty.sh"
        empty.write_text("")
        assert is_script_command(f"{empty}")

    def test_unreadable_file_stays_in_script_mode(self, tmp_path: Path):
        script = tmp_path / "locked.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0)
        if os.access(script, os.R_OK):
            pytest.skip("this user can read a mode 0 file")
        assert is_script_command(f"{script}")
        assert script_fingerprint(f"{script}") == (
            "unreadable:PermissionError"
        )
        temp_dir = create_temp_dir()
        try:
            with pytest.raises(PermissionError):
                rewrite_command(f"{script}", 9999, temp_dir=temp_dir)
        finally:
            cleanup_temp_dir(temp_dir)


class TestRegexEdgeCases:

    def test_double_dash_embedded_not_matched(self):
        """--some--port does not match (lookbehind guard)."""
        assert _PORT_FLAGS_RE.search("--some--port 8080") is None

    def test_listen_port_is_matched(self):
        """--listen-port 8080 is matched (double dash before 'listen')."""
        match = _PORT_FLAGS_RE.search("--listen-port 8080")
        assert match is not None

    def test_listen_port_eq_is_matched(self):
        """--listen-port=8080 is matched."""
        match = _PORT_FLAGS_RE.search("--listen-port=8080")
        assert match is not None

    def test_listen_port_with_single_dash_not_matched(self):
        """-listen-port (single leading dash) not matched."""
        assert _PORT_FLAGS_RE.search("-listen-port 8080") is None

    def test_port_without_value_not_matched(self):
        """--port with no value does not match."""
        assert _PORT_FLAGS_RE.search("--port") is None

    def test_port_with_spaces_and_value(self):
        """--port   8080 (multiple spaces) matches."""
        match = _PORT_FLAGS_RE.search("--port   8080")
        assert match is not None

    def test_port_eq_no_space(self):
        """--port=8080 (no space) matches."""
        match = _PORT_FLAGS_RE.search("--port=8080")
        assert match is not None

    def test_port_eq_with_spaces(self):
        """--port = 8080 (spaces around =) matches."""
        match = _PORT_FLAGS_RE.search("--port = 8080")
        assert match is not None


class TestTempDir:

    def test_create_temp_dir(self):
        """create_temp_dir returns a new directory."""
        temp_dir = create_temp_dir()
        assert temp_dir.exists()
        assert temp_dir.is_dir()
        cleanup_temp_dir(temp_dir)

    def test_cleanup_temp_dir(self):
        """cleanup_temp_dir removes the directory."""
        temp_dir = create_temp_dir()
        cleanup_temp_dir(temp_dir)
        assert not temp_dir.exists()

    def test_cleanup_nonexistent_dir(self):
        """cleanup_temp_dir does not fail on nonexistent dir."""
        cleanup_temp_dir(Path("/tmp/model-switcher-nonexistent"))
        # no exception


class TestTempScriptPerModel:
    """P-20261013-01: entries sharing one script get separate temp copies."""

    def test_model_ids_sharing_a_script_get_separate_copies(self, tmp_path: Path):
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nllama-server --port 8000\n")
        script.chmod(0o755)

        temp_dir = create_temp_dir()
        try:
            cmd_a, temp_a = rewrite_command(
                str(script), 1111, temp_dir=temp_dir, model_id="qwen@text"
            )
            cmd_b, temp_b = rewrite_command(
                str(script), 2222, temp_dir=temp_dir, model_id="qwen@vision"
            )
            assert temp_a != temp_b
            assert temp_a.parent == temp_b.parent == temp_dir
            assert "--port 1111" in temp_a.read_text()
            assert "--port 2222" in temp_b.read_text()
            assert cmd_a == str(temp_a) and cmd_b == str(temp_b)
            assert temp_a.name.startswith("start.sh-qwen_text-")
            assert temp_b.name.startswith("start.sh-qwen_vision-")
            assert temp_a.name.endswith(f"-{os.getpid()}")
        finally:
            cleanup_temp_dir(temp_dir)

    def test_ids_that_sanitize_alike_still_differ(self, tmp_path: Path):
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nserve --port 8000\n")
        temp_dir = create_temp_dir()
        try:
            _, first = rewrite_command(
                str(script), 1, temp_dir=temp_dir, model_id="a@b"
            )
            _, second = rewrite_command(
                str(script), 2, temp_dir=temp_dir, model_id="a/b"
            )
            assert first != second
            assert "@" not in first.name
            assert first.parent == temp_dir and second.parent == temp_dir
        finally:
            cleanup_temp_dir(temp_dir)

    @pytest.mark.parametrize("model_id", ["../x", "../../x@y", "/tmp/x", "a/../b"])
    def test_model_id_cannot_move_the_copy_out_of_the_temp_dir(
        self, tmp_path: Path, model_id: str
    ):
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nserve --port 8000\n")
        temp_dir = create_temp_dir()
        try:
            _, copy = rewrite_command(
                str(script), 1, temp_dir=temp_dir, model_id=model_id
            )
            assert copy.parent == temp_dir
            assert copy.exists()
        finally:
            cleanup_temp_dir(temp_dir)

    def test_reload_generation_gives_a_new_name_and_keeps_old_copy(
        self, tmp_path: Path
    ):
        """P-20261013-04: a reload never overwrites an existing copy."""
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nserve --port 8000\n")
        temp_dir = create_temp_dir()
        try:
            _, first = rewrite_command(
                str(script), 1111, temp_dir=temp_dir, model_id="a"
            )
            _, second = rewrite_command(
                str(script), 2222, temp_dir=temp_dir, model_id="a",
                generation=1,
            )
            _, third = rewrite_command(
                str(script), 3333, temp_dir=temp_dir, model_id="a",
                generation=2,
            )
            assert len({first, second, third}) == 3
            assert "-r" not in first.name.removeprefix("start.sh-a-")
            assert second.name.endswith(f"-r1-{os.getpid()}")
            assert third.name.endswith(f"-r2-{os.getpid()}")
            assert "--port 1111" in first.read_text()
            assert "--port 2222" in second.read_text()
            assert "--port 3333" in third.read_text()
        finally:
            cleanup_temp_dir(temp_dir)

    def test_without_model_id_keeps_old_name(self, tmp_path: Path):
        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nserve --port 8000\n")
        temp_dir = create_temp_dir()
        try:
            _, temp = rewrite_command(str(script), 1, temp_dir=temp_dir)
            assert temp.name == f"start.sh-{os.getpid()}"
        finally:
            cleanup_temp_dir(temp_dir)

    async def test_swapper_rewrites_each_variant_of_a_shared_script(
        self, tmp_path: Path
    ):
        import yaml

        from src.config import load_config
        from src.swapper import Swapper

        script = tmp_path / "start.sh"
        script.write_text("#!/bin/sh\nserve --port 8000 \"$@\"\n")
        script.chmod(0o755)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({
            "m": {"variants": {
                "text": f"{script} --mode text",
                "vision": f"{script} --mode vision",
            }},
            "other": str(script),
        }))
        swapper = Swapper(load_config(path))
        try:
            models = swapper.config.models
            scripts = {
                model_id: Path(mc.cmd.split()[0])
                for model_id, mc in models.items()
            }
            assert len(set(scripts.values())) == 3
            for model_id, temp in scripts.items():
                assert temp.parent == swapper._temp_dir
                assert f"--port {models[model_id].port}" in temp.read_text()
            assert models["m@text"].cmd.endswith("--mode text")
            assert models["m@vision"].cmd.endswith("--mode vision")
        finally:
            await swapper.shutdown()
