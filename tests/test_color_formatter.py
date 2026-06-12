"""Tests for src/color_formatter.py — color-coded log output.

Derived from plan P-20260612-05 acceptance criteria:
- Level-based coloring (INFO/WARNING/ERROR)
- Key event pattern coloring (swap, start, stop, health)
- Auto-disable on non-TTY
- Auto-disable on NO_COLOR env var
- Passthrough when disabled (no ANSI codes)
"""

from __future__ import annotations

import logging
import os
import sys
from unittest import mock

from src.color_formatter import (
    ColorFormatter,
    COLOR_EVENTS,
    LEVEL_COLORS,
    _C,
)

DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _make_formatter() -> ColorFormatter:
    """Create a ColorFormatter with the same format as cli.py."""
    return ColorFormatter(DEFAULT_FMT, DEFAULT_DATEFMT)


def _make_record(level: int, message: str) -> logging.LogRecord:
    """Create a LogRecord for testing."""
    record = logging.LogRecord(
        name="test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    return record


class TestLevelBasedColoring:

    def test_info_gets_green(self):
        """INFO level messages get green color."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.INFO, "hello world")
        output = formatter.format(record)
        assert _C.GREEN in output
        assert _C.RESET in output

    def test_warning_gets_yellow(self):
        """WARNING level messages get yellow color."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.WARNING, "something odd")
        output = formatter.format(record)
        assert _C.YELLOW in output

    def test_error_gets_red(self):
        """ERROR level messages get red color."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.ERROR, "bad thing happened")
        output = formatter.format(record)
        assert _C.RED in output

    def test_critical_gets_red(self):
        """CRITICAL level messages get red color."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.CRITICAL, "fatal error")
        output = formatter.format(record)
        assert _C.RED in output

    def test_debug_gets_no_color(self):
        """DEBUG level messages get no color (not in LEVEL_COLORS)."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.DEBUG, "debug info")
        output = formatter.format(record)
        assert _C.GREEN not in output
        assert _C.YELLOW not in output
        assert _C.RED not in output


class TestKeyEventColoring:

    def test_swapping_event_gets_cyan(self):
        """'Swapping from X to Y' gets cyan override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(
            logging.INFO, "Swapping from model-a to model-b"
        )
        output = formatter.format(record)
        assert _C.CYAN in output

    def test_starting_model_gets_cyan(self):
        """'Starting model X' gets cyan override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.INFO, "Starting model llama-3.1-8b")
        output = formatter.format(record)
        assert _C.CYAN in output

    def test_stopping_with_sigterm_gets_magenta(self):
        """'Stopping X with SIGTERM' gets magenta override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(
            logging.INFO, "Stopping llama-3.1-8b (pid 1234) with SIGTERM"
        )
        output = formatter.format(record)
        assert _C.MAGENTA in output

    def test_health_ready_gets_green(self):
        """'Upstream on port X is ready' gets green override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(
            logging.INFO, "Upstream on port 8081 is ready (/health)"
        )
        output = formatter.format(record)
        assert _C.GREEN in output

    def test_health_not_ready_gets_red(self):
        """'did not become ready' gets red override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(
            logging.ERROR,
            "Model llama on port 8081 did not become ready",
        )
        output = formatter.format(record)
        assert _C.RED in output

    def test_sigkill_warning_stays_yellow(self):
        """'did not exit in Ns, sending SIGKILL' gets yellow override."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(
            logging.WARNING,
            "llama-3.1-8b (pid 1234) did not exit in 3s, sending SIGKILL",
        )
        output = formatter.format(record)
        assert _C.YELLOW in output

    def test_event_pattern_overrides_level(self):
        """Key event pattern takes precedence over level color."""
        formatter = _make_formatter()
        formatter._enabled = True
        # ERROR level normally = red, but 'Starting model' = cyan
        record = _make_record(logging.ERROR, "Starting model llama")
        output = formatter.format(record)
        assert _C.CYAN in output
        assert _C.RED not in output


class TestDisableOnNonTty:

    def test_disabled_when_stdout_not_tty(self):
        """Colors disabled when stdout is not a TTY."""
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            formatter = _make_formatter()
            assert not formatter._enabled

        record = _make_record(logging.INFO, "hello")
        output = formatter.format(record)
        assert _C.GREEN not in output
        assert _C.RESET not in output

    def test_enabled_when_stdout_is_tty(self):
        """Colors enabled when stdout is a TTY and NO_COLOR not set."""
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True):
            formatter = _make_formatter()
            assert formatter._enabled


class TestDisableOnNoColorEnv:

    def test_disabled_when_no_color_set(self):
        """Colors disabled when NO_COLOR env var is set."""
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            formatter = _make_formatter()
            assert not formatter._enabled

        record = _make_record(logging.INFO, "hello")
        output = formatter.format(record)
        assert _C.GREEN not in output

    def test_enabled_when_no_color_not_set(self):
        """Colors enabled when NO_COLOR is not set and stdout is TTY."""
        with mock.patch.object(sys.stdout, "isatty", return_value=True):
            env = os.environ.copy()
            env.pop("NO_COLOR", None)
            with mock.patch.dict(os.environ, env, clear=True):
                formatter = _make_formatter()
                assert formatter._enabled


class TestPassthrough:

    def test_format_preserves_message_when_disabled(self):
        """Message content is preserved when colors are disabled."""
        formatter = _make_formatter()
        formatter._enabled = False
        record = _make_record(logging.INFO, "hello world")
        output = formatter.format(record)
        assert "hello world" in output
        assert "\033[" not in output  # no ANSI codes leaked

    def test_format_preserves_level_when_disabled(self):
        """Level name is preserved when colors are disabled."""
        formatter = _make_formatter()
        formatter._enabled = False
        record = _make_record(logging.ERROR, "bad thing")
        output = formatter.format(record)
        assert "ERROR" in output
        assert "\033[" not in output  # no ANSI codes leaked

    def test_format_with_args_when_disabled(self):
        """Message with %s args is preserved when colors are disabled."""
        formatter = _make_formatter()
        formatter._enabled = False
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "hello world" in output
        assert "\033[" not in output

    def test_format_empty_message(self):
        """Empty message does not crash and has no ANSI when disabled."""
        formatter = _make_formatter()
        formatter._enabled = False
        record = _make_record(logging.INFO, "")
        output = formatter.format(record)
        assert "\033[" not in output

    def test_format_empty_message_with_colors(self):
        """Empty message does not crash and has no color when enabled."""
        formatter = _make_formatter()
        formatter._enabled = True
        record = _make_record(logging.INFO, "")
        output = formatter.format(record)
        # empty message has no matching event pattern, falls back to level color
        assert _C.GREEN in output  # INFO = green


class TestColorConstants:

    def test_level_colors_defined(self):
        """LEVEL_COLORS has entries for INFO, WARNING, ERROR, CRITICAL."""
        assert "INFO" in LEVEL_COLORS
        assert "WARNING" in LEVEL_COLORS
        assert "ERROR" in LEVEL_COLORS
        assert "CRITICAL" in LEVEL_COLORS

    def test_color_events_not_empty(self):
        """COLOR_EVENTS has at least one pattern."""
        assert len(COLOR_EVENTS) >= 1

    def test_all_event_patterns_are_compiled(self):
        """All COLOR_EVENTS entries are (Pattern, str) tuples."""
        import re
        for entry in COLOR_EVENTS:
            pattern, color = entry
            assert isinstance(pattern, re.Pattern)
            assert isinstance(color, str)
            assert color.startswith("\033[")
