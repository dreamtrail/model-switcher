"""Shared helpers for the process-manager tests.

Wait on marker files, child PID files, and process exit. Used by
tests/test_process_manager_lifecycle.py and tests/test_shutdown_hook.py.
"""

from __future__ import annotations

import asyncio
import os


async def _wait_for_path(path, timeout: float = 5) -> None:
    """Wait for a test process to create a marker file."""
    loop = asyncio.get_running_loop()
    end_time = loop.time() + timeout
    while loop.time() < end_time:
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"marker was not written: {path}")


async def _wait_for_pid_file(path, timeout: float = 3) -> int:
    """Wait for a test process to write a child PID file."""
    loop = asyncio.get_running_loop()
    end_time = loop.time() + timeout
    while loop.time() < end_time:
        if path.exists():
            return int(path.read_text())
        await asyncio.sleep(0.05)
    raise AssertionError(f"PID file was not written: {path}")


def _pid_exists(pid: int) -> bool:
    """Return True if a PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _wait_for_pid_gone(pid: int, timeout: float = 3) -> bool:
    """Wait until a PID no longer exists."""
    loop = asyncio.get_running_loop()
    end_time = loop.time() + timeout
    while loop.time() < end_time:
        if not _pid_exists(pid):
            return True
        await asyncio.sleep(0.05)
    return not _pid_exists(pid)

