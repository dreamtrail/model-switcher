"""Tests for src/process_manager.py — upstream process lifecycle.

Derived from plan acceptance criteria:
- Process manager: start/stop, SIGTERM+3s->SIGKILL, log prefix
- Upstream logs prefixed with [model-name] on stdout
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from src.process_manager import (
    GRACE_PERIOD,
    is_running,
    start_process,
    stop_process,
)


class TestStartProcess:

    async def test_start_returns_process_handle(self):
        """Starting a valid command returns a ProcessHandle."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(10)'", 0
        )
        try:
            assert handle.model_name == "test-model"
            assert handle.port == 0
            assert handle.process is not None
            assert handle.process.returncode is None
        finally:
            await stop_process(handle)

    async def test_start_sets_stdout_stderr_tasks(self):
        """Started process has stdout/stderr drain tasks."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(10)'", 0
        )
        try:
            assert handle._stdout_task is not None
            assert handle._stderr_task is not None
        finally:
            await stop_process(handle)

    async def test_start_invalid_command_raises(self):
        """Starting a nonexistent command raises."""
        with pytest.raises((FileNotFoundError, ValueError, OSError)):
            await start_process(
                "bad-model", "nonexistent_binary_xyz --port 9999", 9999
            )


class TestStopProcess:

    async def test_stop_terminates_process(self):
        """stop_process terminates a running process."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(60)'", 0
        )
        await stop_process(handle)
        assert handle.process.returncode is not None
        assert not is_running(handle)

    async def test_stop_already_exited_is_idempotent(self, caplog):
        """Stopping an already-exited process does not raise
        and logs the exit code."""
        import logging
        caplog.set_level(logging.INFO)

        handle = await start_process(
            "test-model", "python -c 'print(\"done\")'", 0
        )
        # Let it finish
        await handle.process.wait()
        # Stopping again should not raise
        await stop_process(handle)
        # Verify the "already exited" log message
        assert any(
            "already exited" in record.message
            for record in caplog.records
        )

    async def test_stop_sends_sigkill_after_grace_period(self, tmp_path):
        """Process that ignores SIGTERM gets SIGKILL after grace period."""
        script = tmp_path / "stubborn.py"
        script.write_text(
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('ready\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        handle = await start_process(
            "stubborn-model",
            f"python {script}",
            0,
        )
        # Wait for the process to set up its signal handler
        await asyncio.sleep(0.5)

        import time
        start = time.monotonic()
        await stop_process(handle)
        elapsed = time.monotonic() - start
        assert elapsed >= GRACE_PERIOD
        assert handle.process.returncode is not None

    async def test_stop_cancels_log_tasks(self):
        """stop_process cancels stdout/stderr drain tasks."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(60)'", 0
        )
        stdout_task = handle._stdout_task
        stderr_task = handle._stderr_task
        await stop_process(handle)
        assert stdout_task.done()
        assert stderr_task.done()


class TestIsRunning:

    async def test_running_returns_true(self):
        """is_running returns True for a live process."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(10)'", 0
        )
        assert is_running(handle) is True
        await stop_process(handle)

    async def test_running_returns_false_after_stop(self):
        """is_running returns False after process is stopped."""
        handle = await start_process(
            "test-model", "python -c 'import time; time.sleep(10)'", 0
        )
        await stop_process(handle)
        assert is_running(handle) is False


class TestLogPiping:

    async def test_stdout_is_captured(self, caplog):
        """Process stdout is captured and logged with model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stdout.write('hello from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify both the prefix and the actual upstream output
        assert any(
            "my-model" in record.message and "hello from model" in record.message
            for record in caplog.records
        )

    async def test_stderr_is_captured(self, caplog):
        """Process stderr is captured and logged with model prefix."""
        import logging
        caplog.set_level(logging.WARNING)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stderr.write('error from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify both the prefix and the actual upstream output
        assert any(
            "my-model" in record.message and "error from model" in record.message
            for record in caplog.records
        )
