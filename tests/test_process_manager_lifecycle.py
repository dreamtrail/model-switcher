"""Tests for src/process_manager.py — upstream process lifecycle.

Covers start, stop, is_running, and per-handle stop_grace.

Derived from plan acceptance criteria:
- AC6/AC10/AC11: confirmed process/group teardown or contextual failure,
  with log-drain finalization and no false stopped state
- Process manager: start/stop, SIGTERM+3s->SIGKILL
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from src.process_manager import (
    GRACE_PERIOD,
    ProcessHandle,
    ProcessStopError,
    is_running,
    start_process,
    stop_process,
)
from tests.process_helpers import (
    _pid_exists,
    _wait_for_path,
    _wait_for_pid_file,
    _wait_for_pid_gone,
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

    async def test_initial_group_probe_oserror_is_contextual_and_cleans_logs(
        self, monkeypatch
    ):
        """An ordinary initial liveness-probe error fails closed (AC6/AC10)."""
        import src.process_manager as process_manager

        class ProbeFailureProcess:
            pid = 4141
            returncode = None

        stdout_task = asyncio.create_task(asyncio.Event().wait())
        stderr_task = asyncio.create_task(asyncio.Event().wait())
        handle = ProcessHandle(
            "probe-failure-model",
            0,
            ProbeFailureProcess(),
            process_group_id=4141,
            _stdout_task=stdout_task,
            _stderr_task=stderr_task,
        )
        probe_error = OSError("probe failed")

        def failed_probe(process_group_id):
            assert process_group_id == 4141
            raise probe_error

        monkeypatch.setattr(
            process_manager, "_process_group_exists", failed_probe
        )

        with pytest.raises(ProcessStopError) as exc_info:
            await stop_process(handle)

        assert exc_info.value.__cause__ is probe_error
        assert str(exc_info.value) == (
            "Could not confirm probe-failure-model (pid 4141) stopped"
        )
        assert handle.stopped is False
        assert stdout_task.done()
        assert stderr_task.done()

    async def test_unconfirmed_stop_raises_and_leaves_handle_unstopped(
        self, monkeypatch
    ):
        """Final unconfirmed parent/group state fails closed (AC6)."""
        import src.process_manager as process_manager

        class UnkillableProcess:
            pid = 4242
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

        stdout_task = asyncio.create_task(asyncio.Event().wait())
        stderr_task = asyncio.create_task(asyncio.Event().wait())
        handle = ProcessHandle(
            "stuck-model",
            0,
            UnkillableProcess(),
            _stdout_task=stdout_task,
            _stderr_task=stderr_task,
        )

        monkeypatch.setattr(
            process_manager, "_signal_process_group", lambda *args: False
        )

        async def never_stopped(*args, **kwargs):
            return False

        monkeypatch.setattr(
            process_manager, "_wait_for_stopped", never_stopped
        )

        with pytest.raises(
            ProcessStopError,
            match=r"stuck-model.*pid 4242.*stopped",
        ):
            await stop_process(handle)

        assert handle.stopped is False
        assert stdout_task.done()
        assert stderr_task.done()

    @pytest.mark.parametrize(
        "failure_site",
        ["terminate", "kill", "group_sigterm", "group_sigkill"],
    )
    async def test_raw_signal_failure_is_normalized_and_cleans_log_tasks(
        self, monkeypatch, failure_site
    ):
        """Raw fallback/group signal errors become ProcessStopError (AC6/AC10)."""
        import src.process_manager as process_manager

        class FailingProcess:
            pid = 4343
            returncode = None

            def terminate(self):
                if failure_site == "terminate":
                    raise PermissionError("terminate denied")

            def kill(self):
                if failure_site == "kill":
                    raise PermissionError("kill denied")

        stdout_task = asyncio.create_task(asyncio.Event().wait())
        stderr_task = asyncio.create_task(asyncio.Event().wait())
        handle = ProcessHandle(
            "signal-failure-model",
            0,
            FailingProcess(),
            process_group_id=4343,
            _stdout_task=stdout_task,
            _stderr_task=stderr_task,
        )

        def signal_group(_handle, sent_signal):
            if (
                failure_site == "group_sigterm"
                and sent_signal == signal.SIGTERM
            ):
                raise PermissionError("group SIGTERM denied")
            if (
                failure_site == "group_sigkill"
                and sent_signal == signal.SIGKILL
            ):
                raise PermissionError("group SIGKILL denied")
            return False

        async def never_stopped(*args, **kwargs):
            return False

        monkeypatch.setattr(
            process_manager, "_signal_process_group", signal_group
        )
        monkeypatch.setattr(
            process_manager, "_wait_for_stopped", never_stopped
        )

        with pytest.raises(ProcessStopError) as exc_info:
            await stop_process(handle)

        assert isinstance(exc_info.value.__cause__, PermissionError)
        assert "signal-failure-model" in str(exc_info.value)
        assert "pid 4343" in str(exc_info.value)
        assert handle.stopped is False
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



class TestStopGrace:
    """P-20261027-01: per-handle wait between SIGTERM and SIGKILL."""

    @staticmethod
    async def _start(tmp_path, body: str):
        ready = tmp_path / "ready"
        script = tmp_path / "server.py"
        script.write_text(
            "import signal, sys, time\n"
            "from pathlib import Path\n"
            f"{body}\n"
            f"Path({str(ready)!r}).touch()\n"
            "time.sleep(60)\n"
        )
        handle = await start_process(
            "grace-model", f"{sys.executable} {script}", 0
        )
        await _wait_for_path(ready)
        return handle

    async def test_shorter_stop_grace_kills_sooner(self, tmp_path):
        handle = await self._start(
            tmp_path, "signal.signal(signal.SIGTERM, signal.SIG_IGN)"
        )
        handle.stop_grace = 0.3
        loop = asyncio.get_running_loop()
        started = loop.time()
        await stop_process(handle)
        elapsed = loop.time() - started
        assert 0.3 <= elapsed < GRACE_PERIOD
        assert handle.process.returncode == -signal.SIGKILL

    async def test_longer_stop_grace_lets_a_slow_stop_finish(
        self, tmp_path, monkeypatch
    ):
        import src.process_manager as pm

        monkeypatch.setattr(pm, "GRACE_PERIOD", 0.2)
        marker = tmp_path / "clean"
        handle = await self._start(tmp_path, (
            "def stop(signum, frame):\n"
            "    time.sleep(0.8)\n"
            f"    Path({str(marker)!r}).write_text('clean')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)"
        ))
        handle.stop_grace = 10
        await stop_process(handle)
        assert handle.process.returncode == 0
        assert marker.read_text() == "clean"

    async def test_unset_stop_grace_uses_grace_period(
        self, tmp_path, monkeypatch
    ):
        import src.process_manager as pm

        monkeypatch.setattr(pm, "GRACE_PERIOD", 0.2)
        marker = tmp_path / "clean"
        handle = await self._start(tmp_path, (
            "def stop(signum, frame):\n"
            "    time.sleep(0.8)\n"
            f"    Path({str(marker)!r}).write_text('clean')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)"
        ))
        assert handle.stop_grace is None
        await stop_process(handle)
        assert handle.process.returncode == -signal.SIGKILL
        assert not marker.exists()

