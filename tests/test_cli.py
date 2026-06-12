"""Tests for src/cli.py — CLI argument parsing and runtime.

Derived from plan acceptance criteria:
- Switcher starts with --config config.yaml --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import sys

import pytest

from src import __version__
from src.cli import _main, parse_args


class TestParseArgs:

    def test_version_flag(self):
        """--version prints version and exits."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--version"])
        assert exc.value.code == 0

    def test_config_required(self):
        """Missing --config raises error."""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_config_accepted(self, tmp_path):
        """--config path is parsed correctly."""
        config_path = tmp_path / "config.yaml"
        args = parse_args(["--config", str(config_path)])
        assert args.config == str(config_path)

    def test_default_host(self):
        """Default host is 127.0.0.1."""
        args = parse_args(["--config", "config.yaml"])
        assert args.host == "127.0.0.1"

    def test_custom_host(self):
        """Custom --host is accepted."""
        args = parse_args(["--config", "config.yaml", "--host", "0.0.0.0"])
        assert args.host == "0.0.0.0"

    def test_default_port(self):
        """Default port is 8080."""
        args = parse_args(["--config", "config.yaml"])
        assert args.port == 8080

    def test_custom_port(self):
        """Custom --port is accepted."""
        args = parse_args(["--config", "config.yaml", "--port", "9000"])
        assert args.port == 9000

    def test_default_timeout(self):
        """Default timeout is 60 seconds."""
        args = parse_args(["--config", "config.yaml"])
        assert args.timeout == 60

    def test_custom_timeout(self):
        """Custom --timeout is accepted."""
        args = parse_args(["--config", "config.yaml", "--timeout", "120"])
        assert args.timeout == 120

    def test_all_flags_combined(self, tmp_path):
        """All flags work together."""
        config_path = tmp_path / "config.yaml"
        args = parse_args([
            "--config", str(config_path),
            "--host", "0.0.0.0",
            "--port", "9292",
            "--timeout", "30",
        ])
        assert args.config == str(config_path)
        assert args.host == "0.0.0.0"
        assert args.port == 9292
        assert args.timeout == 30


class TestMainRuntime:

    async def test_main_config_error_exits_1(self):
        """_main exits with code 1 when config file does not exist.

        Tests the config error handling path: FileNotFoundError → sys.exit(1).
        """
        with pytest.raises(SystemExit) as exc:
            await _main(["--config", "nonexistent_file.yaml"])
        assert exc.value.code == 1

    async def test_main_config_value_error_exits_1(self, tmp_path):
        """_main exits with code 1 when config has invalid content.

        Tests the ValueError path (e.g., non-dict YAML) → sys.exit(1).
        """
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("- model-a\n- model-b\n")  # list, not mapping
        with pytest.raises(SystemExit) as exc:
            await _main(["--config", str(bad_config)])
        assert exc.value.code == 1
