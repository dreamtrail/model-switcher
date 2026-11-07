"""Tests for src/cli.py — CLI argument parsing and runtime.

Derived from plan acceptance criteria:
- Switcher starts with --config config.yaml --host 0.0.0.0 --port 8080
- --idle-timeout accepts duration strings and is passed to Swapper
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from src import __version__
import src.cli as cli_module
from src.cli import _main, parse_args
from src.config import Config, ModelConfig


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

    def test_quick_switch_defaults_off_and_can_be_enabled(self):
        """--quick-switch is a boolean opt-in and defaults off (AC1)."""
        assert parse_args(["--config", "config.yaml"]).quick_switch is False
        assert parse_args([
            "--config", "config.yaml", "--quick-switch",
        ]).quick_switch is True

    def test_automatic_switch_defaults_off_and_can_be_enabled(self):
        """--automatic-switch is a boolean opt-in, off by default."""
        assert parse_args(["--config", "config.yaml"]).automatic_switch is False
        assert parse_args([
            "--config", "config.yaml", "--automatic-switch",
        ]).automatic_switch is True

    def test_automatic_switch_is_documented_in_help(self, capsys):
        """CLI help names the flag and its default-off policy."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"])
        assert exc.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "--automatic-switch" in help_text
        assert "default: off" in help_text
        assert "loads its model when none is running" in help_text

    def test_quick_switch_is_documented_in_help(self, capsys):
        """CLI help documents supersession and the default-off policy (AC1)."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"])
        assert exc.value.code == 0
        help_text = capsys.readouterr().out
        assert "--quick-switch" in help_text
        assert "supersede" in help_text
        assert "default: off" in help_text

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

    def test_sse_keepalive_defaults_to_15_seconds(self):
        """--sse-keepalive is on at 15 s by default (P-20261028-01 AC8)."""
        assert parse_args(["--config", "config.yaml"]).sse_keepalive == 15

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("0", 0.0), ("0.5", 0.5), ("10", 10.0), ("2.25", 2.25)],
    )
    def test_sse_keepalive_accepts_zero_and_fractions(self, value, expected):
        """--sse-keepalive takes 0 (off) and fractional seconds (AC8)."""
        args = parse_args([
            "--config", "config.yaml", "--sse-keepalive", value,
        ])
        assert args.sse_keepalive == expected

    @pytest.mark.parametrize(
        "value", ["-1", "-0.1", "abc", "", "nan", "inf", "10s"],
    )
    def test_sse_keepalive_invalid_values_exit_2(self, value):
        """Negative or non-numeric --sse-keepalive is a usage error (AC8)."""
        with pytest.raises(SystemExit) as exc:
            parse_args([
                "--config", "config.yaml", "--sse-keepalive", value,
            ])
        assert exc.value.code == 2

    def test_sse_keepalive_is_documented_in_help(self, capsys):
        """CLI help names the flag, its default, and how to disable it."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"])
        assert exc.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "--sse-keepalive SECONDS" in help_text
        assert "default: 15; 0 disables" in help_text

    def test_upstream_keepalive_defaults_to_4_seconds(self):
        """--upstream-keepalive is 4 s by default (P-20261107-01)."""
        args = parse_args(["--config", "config.yaml"])
        assert args.upstream_keepalive == 4.0

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("0.5", 0.5), ("2", 2.0), ("10", 10.0), ("1.25", 1.25)],
    )
    def test_upstream_keepalive_accepts_positive_seconds(
        self, value, expected,
    ):
        """--upstream-keepalive takes positive and fractional seconds."""
        args = parse_args([
            "--config", "config.yaml", "--upstream-keepalive", value,
        ])
        assert args.upstream_keepalive == expected

    @pytest.mark.parametrize(
        "value", ["0", "0.0", "-1", "-0.1", "abc", "", "nan", "inf", "4s"],
    )
    def test_upstream_keepalive_invalid_values_exit_2(self, value):
        """Zero, negative, or non-numeric values are a usage error."""
        with pytest.raises(SystemExit) as exc:
            parse_args([
                "--config", "config.yaml", "--upstream-keepalive", value,
            ])
        assert exc.value.code == 2

    def test_upstream_keepalive_is_documented_in_help(self, capsys):
        """CLI help names the flag and its default."""
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"])
        assert exc.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "--upstream-keepalive SECONDS" in help_text
        assert "(default: 4)" in help_text

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
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config
                self.timeout = timeout
                self.idle_timeout = idle_timeout
                self.quick_switch = quick_switch
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
        # P-20261013-04: absolute config path kept for control reload.
        assert created_swappers[0].config_path == Path("config.yaml").resolve()
        assert created_swappers[0].config_path.is_absolute()
        assert created_swappers[0].timeout == 60
        assert created_swappers[0].idle_timeout == expected_idle_timeout
        assert created_swappers[0].quick_switch is False
        assert isinstance(created_swappers[0].client, FakeClientSession)

    @pytest.mark.parametrize(
        ("extra_args", "expected_quick_switch"),
        [([], False), (["--quick-switch"], True)],
    )
    async def test_main_passes_quick_switch_to_swapper(
        self, monkeypatch, extra_args, expected_quick_switch
    ):
        """_main wires the exact parsed quick-switch boolean (AC1)."""
        captured = {}

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config
                captured["quick_switch"] = quick_switch

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            pass

        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml", *extra_args])

        assert captured == {"quick_switch": expected_quick_switch}

    @pytest.mark.parametrize(
        ("extra_args", "expected_automatic_switch"),
        [([], False), (["--automatic-switch"], True)],
    )
    async def test_main_passes_automatic_switch_to_swapper(
        self, monkeypatch, extra_args, expected_automatic_switch
    ):
        """_main wires the parsed --automatic-switch boolean into Swapper."""
        captured = {}

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config
                captured["automatic_switch"] = automatic_switch

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            pass

        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml", *extra_args])

        assert captured == {"automatic_switch": expected_automatic_switch}

    @pytest.mark.parametrize(
        ("extra_args", "expected_keepalive"),
        [([], 15.0), (["--sse-keepalive", "0"], 0.0),
         (["--sse-keepalive", "2.5"], 2.5)],
    )
    async def test_main_passes_sse_keepalive_to_swapper(
        self, monkeypatch, extra_args, expected_keepalive
    ):
        """_main wires the parsed --sse-keepalive into Swapper (AC8)."""
        captured = {}

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config
                captured["sse_keepalive"] = sse_keepalive

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            pass

        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml", *extra_args])

        assert captured == {"sse_keepalive": expected_keepalive}

    @pytest.mark.parametrize(
        ("extra_args", "expected_keepalive"),
        [([], 4.0), (["--upstream-keepalive", "2.5"], 2.5)],
    )
    async def test_main_passes_upstream_keepalive_to_connector(
        self, monkeypatch, extra_args, expected_keepalive
    ):
        """_main builds the shared session on a connector with the value."""
        captured = {}

        class FakeConnector:
            def __init__(self, *args, **kwargs):
                captured["connector_kwargs"] = kwargs

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                captured["session_connector"] = kwargs.get("connector")

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config
                captured["client"] = client

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            pass

        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "TCPConnector", FakeConnector)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml", *extra_args])

        assert captured["connector_kwargs"] == {
            "keepalive_timeout": expected_keepalive,
        }
        assert isinstance(captured["session_connector"], FakeConnector)
        assert isinstance(captured["client"], FakeClientSession)

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
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
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
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
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

    async def test_main_registers_sighup_and_it_reloads_without_exiting(
        self, monkeypatch, caplog
    ):
        """SIGHUP runs the control reload; it never starts a shutdown."""
        registered_handlers = {}
        reload_calls = []

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config

            def control_reload_config(self):
                reload_calls.append(1)
                return "success", {
                    "added": ["b"], "removed": [], "changed": ["a"],
                    "running_changed": False,
                }

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            registered_handlers[signal.SIGHUP]()
            registered_handlers[signal.SIGHUP]()
            assert shutdown_event.is_set() is False
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

        with caplog.at_level("INFO", logger="src.cli"):
            await cli_module._main(["--config", "config.yaml"])

        assert reload_calls == [1, 1]
        assert "SIGHUP: config reloaded (added 1, removed 0, changed 1)" in (
            caplog.text
        )

    async def test_sighup_after_shutdown_started_is_skipped(
        self, monkeypatch, caplog
    ):
        registered_handlers = {}
        reload_calls = []

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config

            def control_reload_config(self):
                reload_calls.append(1)
                return "success", {}

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            registered_handlers[signal.SIGTERM]()
            registered_handlers[signal.SIGHUP]()

        def fake_add_signal_handler(sig, callback):
            registered_handlers[sig] = callback

        loop = asyncio.get_running_loop()
        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        with caplog.at_level("INFO", logger="src.cli"):
            await cli_module._main(["--config", "config.yaml"])

        assert reload_calls == []
        assert "SIGHUP: reload skipped, shutdown in progress" in caplog.text

    async def test_missing_sighup_support_does_not_break_startup(
        self, monkeypatch
    ):
        """Where add_signal_handler is unsupported, startup still works."""
        ran = []

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSwapper:
            def __init__(
                self, config, *, timeout, idle_timeout, quick_switch,
                automatic_switch, sse_keepalive, client,
            ):
                self.config = config

            async def shutdown(self):
                pass

        async def fake_run_server(host, port, swapper, shutdown_event):
            ran.append(1)

        def refuse(sig, callback):
            raise NotImplementedError

        loop = asyncio.get_running_loop()
        config = type("Config", (), {"models": {"model-a": object()}})()
        monkeypatch.setattr(loop, "add_signal_handler", refuse)
        monkeypatch.setattr(cli_module, "load_config", lambda path: config)
        monkeypatch.setattr(cli_module.aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(cli_module, "Swapper", FakeSwapper)
        monkeypatch.setattr(cli_module, "_run_server", fake_run_server)

        await cli_module._main(["--config", "config.yaml"])

        assert ran == [1]

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


class TestReloadOnSighup:
    """reload_on_sighup logs each outcome and never raises."""

    class _Swapper:
        def __init__(self, result):
            self.result = result

        def control_reload_config(self):
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    @pytest.mark.parametrize(
        ("result", "level", "text"),
        [
            (
                ("success", {"added": [], "removed": ["x"], "changed": [],
                             "running_changed": True}),
                "INFO",
                "SIGHUP: config reloaded (added 0, removed 1, changed 0; "
                "the running model keeps its old settings until it stops)",
            ),
            (
                ("config_invalid", "Model 'a' has an empty command"),
                "WARNING",
                "SIGHUP: config not reloaded: Model 'a' has an empty command",
            ),
            (
                ("control_busy", None), "WARNING",
                "SIGHUP: reload skipped because a model is loading or "
                "unloading; send SIGHUP again when it is done",
            ),
            (
                ("shutting_down", None), "INFO",
                "SIGHUP: reload skipped, the switcher is shutting down",
            ),
            (("other", None), "WARNING", "SIGHUP: reload skipped (other)"),
            (
                RuntimeError("boom"), "ERROR",
                "SIGHUP: config reload crashed; old config kept",
            ),
        ],
    )
    def test_outcomes_are_logged(self, caplog, result, level, text):
        with caplog.at_level("INFO", logger="src.cli"):
            cli_module.reload_on_sighup(self._Swapper(result))
        records = [r for r in caplog.records if r.name == "src.cli"]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            (level, text)
        ]


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
            # Real Config: the startup log lists its public names.
            config = Config(models={"model-a": ModelConfig("model-a", "x")})

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
