"""Tests for src/process_manager.py — upstream process lifecycle.

Derived from plan acceptance criteria:
- AC6/AC10/AC11: confirmed process/group teardown or contextual failure,
  with log-drain finalization and no false stopped state
- Process manager: start/stop, SIGTERM+3s->SIGKILL
- Upstream logs captured without repeated model-name prefixes
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest
import logging

from src.process_manager import (
    GRACE_PERIOD,
    ProcessHandle,
    ProcessStopError,
    _log_upstream_line,
    _upstream_log_level,
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


class TestLogPiping:

    async def test_stdout_is_captured(self, caplog):
        """Process stdout is captured and logged without model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stdout.write('hello from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify the upstream output is logged plainly without model prefix.
        assert any(
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "hello from model"
            for record in caplog.records
        )

    async def test_stderr_is_captured_as_info_by_default(self, caplog):
        """Process stderr is captured as plain INFO without model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stderr.write('plain from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify the upstream output is logged plainly without model prefix.
        assert any(
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "plain from model"
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
            and record.message == "13.31.779.183 W slot update_slots"
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
            and record.message == "13.31.779.183 E load failed"
            for record in caplog.records
        )

class TestUpstreamLogLevel:
    """Unit tests for _upstream_log_level() regex matching."""

    # --- vLLM format ---

    def test_vllm_info_full_line(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) INFO 06-21 20:55:26 [gpu_model_runner.py:6585] message"
        )
        assert level == logging.INFO

    def test_vllm_warning(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) WARNING 06-21 20:55:27 [model.py:1502] Default parameters"
        )
        assert level == logging.WARNING

    def test_vllm_error(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) ERROR 06-21 20:55:26 [core.py:306] load failed"
        )
        assert level == logging.ERROR

    def test_vllm_api_server_info(self):
        level = _upstream_log_level(
            "(APIServer pid=16821) INFO 06-21 20:55:26 [api_server.py:579] ready"
        )
        assert level == logging.INFO

    def test_vllm_no_level_keyword(self):
        """Line with component prefix but no level keyword defaults to INFO."""
        level = _upstream_log_level(
            "(EngineCore pid=16894) Loading model weights..."
        )
        assert level == logging.INFO

    # --- llama.cpp format (regression) ---

    def test_llama_warning_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 W slot update_slots")
        assert level == logging.WARNING

    def test_llama_error_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 E load failed")
        assert level == logging.ERROR

    def test_llama_info_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 I model loaded")
        assert level == logging.INFO

    def test_llama_info_at_position_zero(self):
        level = _upstream_log_level("INFO [init] initializing slots")
        assert level == logging.INFO

    def test_llama_warn_at_position_zero(self):
        level = _upstream_log_level("WARN [update_slots] all slots are idle")
        assert level == logging.WARNING

    # --- edge cases ---

    def test_plain_text_defaults_to_info(self):
        level = _upstream_log_level("some plain text with no markers")
        assert level == logging.INFO

    def test_level_keyword_midline_not_matched(self):
        """Level keyword in message body should not be matched."""
        level = _upstream_log_level("This WARNING is part of the message")
        assert level == logging.INFO

    # --- missing severity markers ---

    def test_f_single_letter_error(self):
        """Single-letter F maps to ERROR."""
        level = _upstream_log_level("F fatal error occurred")
        assert level == logging.ERROR

    def test_fatal_keyword(self):
        """FATAL keyword maps to ERROR."""
        level = _upstream_log_level("FATAL system crash")
        assert level == logging.ERROR

    def test_critical_keyword(self):
        """CRITICAL keyword maps to ERROR."""
        level = _upstream_log_level("CRITICAL disk failure")
        assert level == logging.ERROR

    # --- empty / whitespace inputs ---

    def test_empty_string_defaults_to_info(self):
        """Empty string defaults to INFO."""
        level = _upstream_log_level("")
        assert level == logging.INFO

    def test_leading_whitespace_before_level(self):
        """Leading whitespace before level keyword is handled."""
        level = _upstream_log_level("  WARNING delayed startup")
        assert level == logging.WARNING

    # --- case insensitivity ---

    def test_lowercase_level_after_vllm_prefix(self):
        """Lowercase level keyword after vLLM prefix matches."""
        level = _upstream_log_level("(EngineCore pid=1) info 06-21 20:55:26 [core.py] msg")
        assert level == logging.INFO

    def test_warn_after_vllm_prefix(self):
        """WARN keyword (not WARNING) after vLLM prefix matches."""
        level = _upstream_log_level("(EngineCore pid=1) WARN 06-21 20:55:27 [model.py] msg")
        assert level == logging.WARNING

    # --- TabbyAPI format (P-20260927-01) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("12:34:56.789 WARNING:  Port 5000 in use", logging.WARNING),
            ("12:34:56.789 ERROR:    Chat completion aborted", logging.ERROR),
            ("12:34:56.789 CRITICAL: out of memory", logging.ERROR),
            ("12:34:56.789 SUCCESS:  Model loaded", logging.INFO),
            ("12:34:56.789 DEBUG:    request body", logging.INFO),
            ("12:34:56.789 TRACE:    sampler state", logging.INFO),
            ("12:34:56.789 INFO:     Loading model", logging.INFO),
            ("WARNING:  Disabling authentication", logging.WARNING),
            ("CRITICAL: fatal", logging.ERROR),
            ("SUCCESS:  ready", logging.INFO),
            ("ERROR:", logging.ERROR),
        ],
    )
    def test_tabby_level_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            # Level inside the message body of an INFO record.
            "12:34:56.789 INFO:     client sent WARNING: nothing",
            # Case-sensitive: lowercase loguru names are not TabbyAPI markers.
            "12:34:56.789 warning: lower case",
            # Level must be directly followed by a colon.
            "12:34:56.789 WARNING  missing colon",
        ],
    )
    def test_tabby_prefix_does_not_promote_other_lines(self, line):
        assert _upstream_log_level(line) == logging.INFO


    # --- Strata format (P-20261005-01) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Verbatim shapes from Strata serve/server.py @ 6f32ec0.
            ("[strata] still starting (42 s) - please wait ...", logging.INFO),
            ("[strata] reading the prompt: 2048 tokens, 3.1 s so far",
             logging.INFO),
            ("[strata] done: 128 tokens in 4.2 s (30.5 tok/s)", logging.INFO),
            ("[strata] refused a request for Host 'dgx1:8080'", logging.INFO),
            ("[strata] stopping ...", logging.INFO),
            ("[strata] WARNING: RAM is tight - 3.2 GiB free", logging.WARNING),
            ("[strata] WARN: low VRAM headroom", logging.WARNING),
            ("[strata] ERROR: engine exited with code 1", logging.ERROR),
            ("[strata] FATAL: could not open the model", logging.ERROR),
            ("[strata] CRITICAL: out of memory", logging.ERROR),
            ("[strata] WARNING: retry after ERROR in engine", logging.ERROR),
            # Lower-case words are message text, not levels.
            ("[strata] the engine reported an error: bad request",
             logging.INFO),
            ("[strata] a warning about nothing", logging.INFO),
            # Word boundaries: no partial matches.
            ("[strata] WARNINGS_SEEN=0 ERRORS=0", logging.INFO),
        ],
    )
    def test_strata_line_levels(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Not "[strata] "-prefixed: the existing rules apply unchanged.
            ("loading the model ...", logging.INFO),
            ("       WARNING: no API key - anyone on this machine can use it",
             logging.WARNING),
            ("ERROR something failed", logging.ERROR),
            ("[strata]WARNING: no space after the tag", logging.INFO),
            ("12:34:56.789 INFO:     [strata] WARNING: quoted", logging.INFO),
        ],
    )
    def test_strata_rule_needs_exact_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    # --- TensorFold format (P-20261005-04) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Shapes from TensorFold src/tensorfold @ 609ca419.
            ("[tensorfold] serving qwen at http://127.0.0.1:8080/v1 "
             "(mlx, loaded in 12.3s)", logging.INFO),
            ("[tensorfold] done req-0123456789ab prompt=300 cached=256 "
             "tokens=3 tok/s=100.0 ttft=0.10s prefill=0.09s rounds=2 "
             "accepted=1/2", logging.INFO),
            ("[tensorfold] warning: non-loopback server has no API key; "
             "requests are open", logging.WARNING),
            ("[tensorfold] WARNING: prefill kernel pin mismatch",
             logging.WARNING),
            ("[tensorfold] Warning: mixed case", logging.WARNING),
            ("[tensorfold] request error: RuntimeError: boom",
             logging.ERROR),
            ("[tensorfold] REQUEST ERROR: upper case", logging.ERROR),
            ("[tensorfold] error: plain error", logging.ERROR),
            ("[tensorfold] Error: mixed case", logging.ERROR),
            # Only the start of the message counts.
            ("[tensorfold] the client saw an error: bad request",
             logging.INFO),
            ("[tensorfold] retry after warning: none", logging.INFO),
            ("[tensorfold] stream error: RuntimeError: boom", logging.INFO),
            ("[tensorfold] snapshot save failed: OSError: disk full",
             logging.INFO),
            # A colon is required right after the word.
            ("[tensorfold] warnings=0 errors=0", logging.INFO),
            ("[tensorfold] error count 0", logging.INFO),
            ("[tensorfold]  warning: two spaces after the tag",
             logging.INFO),
        ],
    )
    def test_tensorfold_line_levels(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Not "[tensorfold] "-prefixed: the existing rules apply.
            ("[tensorfold]warning: no space after the tag", logging.INFO),
            ("[TensorFold] warning: other case tag", logging.INFO),
            ("  [tensorfold] error: indented", logging.INFO),
            # Traceback lines on stderr carry no prefix.
            ("Traceback (most recent call last):", logging.INFO),
            ("RuntimeError: boom", logging.INFO),
            ("WARNING: generic rule still applies", logging.WARNING),
            ("ERROR something failed", logging.ERROR),
        ],
    )
    def test_tensorfold_rule_needs_exact_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    def test_tensorfold_info_lines_stay_plain_upstream(self, caplog):
        caplog.set_level(logging.INFO, logger="src.process_manager")
        _log_upstream_line("[tensorfold] done req-1 prompt=3 tokens=1")
        _log_upstream_line("[tensorfold] request error: RuntimeError: boom")
        info, error = caplog.records[-2:]
        assert info.levelno == logging.INFO
        assert getattr(info, "plain_upstream", False) is True
        assert error.levelno == logging.ERROR
        assert getattr(error, "plain_upstream", False) is False


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
