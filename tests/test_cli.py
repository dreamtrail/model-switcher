"""Tests for src/cli.py — CLI argument parsing and runtime.

Derived from plan acceptance criteria:
- Switcher starts with --config config.yaml --host 0.0.0.0 --port 8080
- --idle-timeout accepts duration strings and is passed to Swapper
"""

from __future__ import annotations

import asyncio
import signal
import sys

import pytest

from src import __version__
import src.cli as cli_module
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

    def test_default_idle_timeout(self):
        """Default idle timeout is 30 minutes."""
        args = parse_args(["--config", "config.yaml"])
        assert args.idle_timeout == 1800

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("900", 900),
            ("900s", 900),
            ("15m", 900),
            ("1h", 3600),
            ("0", 0),
            ("0s", 0),
            ("0m", 0),
            ("0h", 0),
        ],
    )
    def test_idle_timeout_duration_formats(self, value, expected):
        """--idle-timeout accepts seconds and s/m/h durations."""
        args = parse_args([
            "--config", "config.yaml",
            "--idle-timeout", value,
        ])
        assert args.idle_timeout == expected

    @pytest.mark.parametrize(
        "value",
        ["-1", "abc", "1.5h", "1h30m", "10d", "m"],
    )
    def test_idle_timeout_invalid_format_exits_2(self, value):
        """Invalid --idle-timeout values are argparse usage errors."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--config", "config.yaml", "--idle-timeout", value])
        assert exc.value.code == 2

    def test_all_flags_combined(self, tmp_path):
        """All flags work together."""
        config_path = tmp_path / "config.yaml"
        args = parse_args([
            "--config", str(config_path),
            "--host", "0.0.0.0",
            "--port", "9292",
            "--timeout", "30",
            "--idle-timeout", "1h",
        ])
        assert args.config == str(config_path)
        assert args.host == "0.0.0.0"
        assert args.port == 9292
        assert args.timeout == 30
        assert args.idle_timeout == 3600


class TestMainRuntime:

    @pytest.mark.parametrize(
        ("extra_args", "expected_idle_timeout"),
        [
            ([], 1800),
            (["--idle-timeout", "1h"], 3600),
            (["--idle-timeout", "0"], 0),
        ],
    )
    async def test_main_passes_idle_timeout_to_swapper(
        self, monkeypatch, extra_args, expected_idle_timeout
    ):
        """_main wires parsed --idle-timeout values into Swapper."""
        created_swappers = []

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class FakeSwapper:
            def __init__(self, config, *, timeout, idle_timeout, client):
                self.config = config
                self.timeout = timeout
                self.idle_timeout = idle_timeout
                self.client = client
                created_swappers.append(self)

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            assert host == "127.0.0.1"
            assert port == 8080
            assert shutdown_event.is_set() is False

        config = type("Config", (), {"models": {"model-a": object()}})()

        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml", *extra_args])

        assert len(created_swappers) == 1
        assert created_swappers[0].config is config
        assert created_swappers[0].timeout == 60
        assert created_swappers[0].idle_timeout == expected_idle_timeout
        assert isinstance(created_swappers[0].client, FakeClientSession)

    async def test_main_first_signal_starts_graceful_shutdown(
        self, monkeypatch
    ):
        """First shutdown signal logs once and sets the shutdown event."""
        registered_handlers = {}
        shutdown_called = False

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(self, config, *, timeout, idle_timeout, client):
                self.config = config

            async def shutdown(self):
                nonlocal shutdown_called
                shutdown_called = True

        async def fake_run_server(host, port, swapper, shutdown_event):
            assert shutdown_event.is_set() is False
            registered_handlers[signal.SIGINT]()
            assert shutdown_event.is_set() is True
            assert asyncio.current_task().cancelling() == 0

        def fake_add_signal_handler(sig, callback):
            registered_handlers[sig] = callback

        loop = asyncio.get_running_loop()
        config = type("Config", (), {"models": {"model-a": object()}})()

        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml"])

        assert signal.SIGINT in registered_handlers
        assert signal.SIGTERM in registered_handlers
        assert shutdown_called is True

    async def test_main_second_signal_cancels_main_task(self, monkeypatch):
        """A repeated shutdown signal escalates by cancelling _main."""
        registered_handlers = {}
        shutdown_called = False

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(self, config, *, timeout, idle_timeout, client):
                self.config = config

            async def shutdown(self):
                nonlocal shutdown_called
                shutdown_called = True

        async def fake_run_server(host, port, swapper, shutdown_event):
            registered_handlers[signal.SIGINT]()
            registered_handlers[signal.SIGINT]()
            await asyncio.sleep(0)

        def fake_add_signal_handler(sig, callback):
            registered_handlers[sig] = callback

        loop = asyncio.get_running_loop()
        config = type("Config", (), {"models": {"model-a": object()}})()

        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        with pytest.raises(asyncio.CancelledError):
            await cli_module._main(["--config", "config.yaml"])

        assert shutdown_called is True

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


class TestRunServer:

    async def test_run_server_uses_bounded_shutdown_timeout(self, monkeypatch):
        """aiohttp graceful shutdown is bounded below its long default."""
        captured = {}

        class FakeRunner:
            def __init__(self, app, **kwargs):
                captured["runner_kwargs"] = kwargs

            async def setup(self):
                pass

            async def cleanup(self):
                captured["cleanup_called"] = True

        class FakeSite:
            def __init__(self, runner, host, port):
                pass

            async def start(self):
                captured["site_started"] = True

        class FakeSwapper:
            config = type("Config", (), {"models": {"model-a": object()}})()

            async def handle_request(self, request):
                raise AssertionError("handler should not be called")

        shutdown_event = asyncio.Event()
        shutdown_event.set()

        monkeypatch.setattr(cli_module.web, "AppRunner", FakeRunner)
        monkeypatch.setattr(cli_module.web, "TCPSite", FakeSite)

        await cli_module._run_server(
            "127.0.0.1", 8080, FakeSwapper(), shutdown_event
        )

        assert captured["runner_kwargs"] == {
            "shutdown_timeout": cli_module.SERVER_SHUTDOWN_TIMEOUT,
        }
        assert cli_module.SERVER_SHUTDOWN_TIMEOUT > 0
        assert captured["site_started"] is True
        assert captured["cleanup_called"] is True
