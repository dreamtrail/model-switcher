"""Tests for src/process_manager.py — upstream process lifecycle.

Derived from plan acceptance criteria:
- Process manager: start/stop, SIGTERM+3s->SIGKILL, log prefix
- Upstream logs prefixed with [model-name] on stdout
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

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
            if os.name == "posix":
                assert handle.process_group_id == handle.process.pid
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

    async def test_stop_kills_child_process_from_wrapper_script(self, tmp_path):
        """stop_process kills child processes in the model process group."""
        wrapper = tmp_path / "wrapper.py"
        child_pid_file = tmp_path / "child.pid"
        wrapper.write_text(
            "import pathlib, subprocess, sys\n"
            "pid_file = pathlib.Path(sys.argv[1])\n"
            "child = subprocess.Popen([\n"
            "    sys.executable, '-c', 'import time; time.sleep(60)'\n"
            "])\n"
            "pid_file.write_text(str(child.pid))\n"
            "print('ready', flush=True)\n"
            "child.wait()\n"
        )

        handle = await start_process(
            "wrapper-model",
            f"{sys.executable} {wrapper} {child_pid_file}",
            0,
        )
        try:
            child_pid = await _wait_for_pid_file(child_pid_file)
            assert _pid_exists(child_pid)
            await stop_process(handle)
            assert handle.process.returncode is not None
            assert await _wait_for_pid_gone(child_pid)
        finally:
            if is_running(handle):
                await stop_process(handle)

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    async def test_stop_kills_child_when_wrapper_already_exited(self, tmp_path):
        """Already-exited wrapper still tears down surviving child group."""
        wrapper = tmp_path / "wrapper_exits.py"
        child_pid_file = tmp_path / "child.pid"
        wrapper.write_text(
            "import pathlib, subprocess, sys\n"
            "pid_file = pathlib.Path(sys.argv[1])\n"
            "child = subprocess.Popen([\n"
            "    sys.executable, '-c', 'import time; time.sleep(60)'\n"
            "])\n"
            "pid_file.write_text(str(child.pid))\n"
            "print('child-started', flush=True)\n"
        )

        handle = await start_process(
            "exited-wrapper-model",
            f"{sys.executable} {wrapper} {child_pid_file}",
            0,
        )
        child_pid = await _wait_for_pid_file(child_pid_file)
        try:
            await handle.process.wait()
            assert handle.process.returncode is not None
            assert _pid_exists(child_pid)

            await stop_process(handle)

            assert await _wait_for_pid_gone(child_pid)
        finally:
            if _pid_exists(child_pid):
                os.kill(child_pid, signal.SIGKILL)
            if is_running(handle):
                await stop_process(handle)


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
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "my-model: hello from model"
            for record in caplog.records
        )

    async def test_stderr_is_captured_as_info_by_default(self, caplog):
        """Process stderr is captured as plain INFO with model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stderr.write('plain from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify both the prefix and the actual upstream output
        assert any(
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "my-model: plain from model"
            for record in caplog.records
        )

    async def test_stderr_warning_marker_is_captured_as_warning(self, caplog):
        """Upstream warning markers are logged as formatted WARNING."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; "
            "sys.stderr.write('13.31.779.183 W slot update_slots\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        assert any(
            record.levelno == logging.WARNING
            and getattr(record, "plain_upstream", False) is False
            and record.message == "my-model: 13.31.779.183 W slot update_slots"
            for record in caplog.records
        )

    async def test_stderr_error_marker_is_captured_as_error(self, caplog):
        """Upstream error markers are logged as formatted ERROR."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; "
            "sys.stderr.write('13.31.779.183 E load failed\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        assert any(
            record.levelno == logging.ERROR
            and getattr(record, "plain_upstream", False) is False
            and record.message == "my-model: 13.31.779.183 E load failed"
            for record in caplog.records
        )


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
