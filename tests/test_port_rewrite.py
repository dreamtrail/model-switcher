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
from pathlib import Path

import pytest

from src.port_rewrite import (
    _PORT_FLAGS_RE,
    cleanup_temp_dir,
    create_temp_dir,
    rewrite_command,
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
