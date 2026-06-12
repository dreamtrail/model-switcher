"""ANSI color formatter for logging output."""

from __future__ import annotations

import logging
import os
import re
import sys


# ANSI escape codes
class _C:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"


# Level → color mapping
LEVEL_COLORS: dict[str, str] = {
    "INFO": _C.GREEN,
    "WARNING": _C.YELLOW,
    "ERROR": _C.RED,
    "CRITICAL": _C.RED,
}

# Key event patterns → color override (checked before level-based)
# Each entry: (compiled regex, ANSI color)
COLOR_EVENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Swapping from .* to "), _C.CYAN),
    (re.compile(r"Starting model "), _C.CYAN),
    (re.compile(r"Stopping .* with SIGTERM"), _C.MAGENTA),
    (re.compile(r"Upstream on port \d+ is ready"), _C.GREEN),
    (re.compile(r"did not become ready"), _C.RED),
    (re.compile(r"did not exit in \d+s, sending SIGKILL"), _C.YELLOW),
]


class ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colors to log output.

    Colors are auto-disabled when stdout is not a TTY or
    the NO_COLOR environment variable is set.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._enabled = (
            sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        )

    def _color_for_record(self, record: logging.LogRecord) -> str | None:
        """Return ANSI color code for a log record, or None if disabled."""
        if not self._enabled:
            return None

        # Check key event patterns first (override level-based)
        message = record.getMessage()
        for pattern, color in COLOR_EVENTS:
            if pattern.search(message):
                return color

        # Fall back to level-based coloring
        return LEVEL_COLORS.get(record.levelname)

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record with optional ANSI color wrapping."""
        color = self._color_for_record(record)
        formatted = super().format(record)
        if color:
            return f"{color}{formatted}{_C.RESET}"
        return formatted
