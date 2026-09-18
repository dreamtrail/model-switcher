"""Upstream process lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

GRACE_PERIOD = 3  # seconds to wait after SIGTERM before SIGKILL


class ProcessStopError(RuntimeError):
    """Raised when a tracked process cannot be confirmed stopped."""


_NINFER_LEVEL_RE = re.compile(
    r"^\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]\s+"
    r"\[(?P<level>INFO|WARNING|ERROR)\]\s+ninfer-serve:",
    re.IGNORECASE,
)

_UPSTREAM_LEVEL_RE = re.compile(
    r"^\s*(?:"
    r"(?:\d+\.)+\d+\s+"  # llama.cpp: dotted timestamp (13.31.779.183)
    r"|"
    r"(?:\(\s*\w[\w\s=()]*\)\s+)"  # vLLM: (Component pid=X)
    r")?"
    r"(?P<level>I|W|E|F|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b",
    re.IGNORECASE,
)


@dataclass
class ProcessHandle:
    """Wrapper around a running upstream process."""

    model_name: str
    port: int
    process: asyncio.subprocess.Process
    process_group_id: int | None = None
    stopped: bool = False
    _stdout_task: asyncio.Task | None = None
    _stderr_task: asyncio.Task | None = None


async def _drain_stream(reader: asyncio.StreamReader) -> None:
    """Read lines from a stream and log them as upstream output."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n\r")
            if text:
                _log_upstream_line(text)
    except asyncio.CancelledError:
        pass


def _log_upstream_line(text: str) -> None:
    """Log an upstream output line at the level indicated by the line."""
    level = _upstream_log_level(text)
    if level == logging.INFO:
        logger.log(level, text, extra={"plain_upstream": True})
        return
    logger.log(level, text)


def _upstream_log_level(text: str) -> int:
    """Infer an upstream log level from common severity markers."""
    match = _NINFER_LEVEL_RE.search(text) or _UPSTREAM_LEVEL_RE.search(text)
    if not match:
        return logging.INFO

    marker = match.group("level").upper()
    if marker in {"W", "WARN", "WARNING"}:
        return logging.WARNING
    if marker in {"E", "F", "ERROR", "FATAL", "CRITICAL"}:
        return logging.ERROR
    return logging.INFO


async def start_process(
    model_name: str, cmd: str, port: int
) -> ProcessHandle:
    """Start an upstream process and pipe its output.

    Args:
        model_name: Human-readable model identifier for lifecycle logs.
        cmd: Shell command to execute.
        port: Port the process should listen on.

    Returns:
        ProcessHandle wrapping the running process.

    Raises:
        subprocess.SubprocessError: if the process fails to start.
    """
    args = shlex.split(cmd)

    logger.info("Starting %s on port %d: %s", model_name, port, cmd)

    start_kwargs = {}
    if os.name == "posix":
        start_kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **start_kwargs,
    )

    stdout_task = asyncio.create_task(
        _drain_stream(process.stdout),
        name=f"stdout-{model_name}",
    )
    stderr_task = asyncio.create_task(
        _drain_stream(process.stderr),
        name=f"stderr-{model_name}",
    )

    handle = ProcessHandle(
        model_name=model_name,
        port=port,
        process=process,
        process_group_id=process.pid if os.name == "posix" else None,
        _stdout_task=stdout_task,
        _stderr_task=stderr_task,
    )
    return handle


async def stop_process(handle: ProcessHandle) -> None:
    """Stop a running upstream process.

    Sends SIGTERM to the process group, waits up to GRACE_PERIOD seconds,
    then SIGKILLs the group. Falls back to direct process signals if group
    signaling is unavailable.
    """
    proc = handle.process
    pid = proc.pid

    signal_error: Exception | None = None
    terminated = False

    try:
        # Keep the initial liveness probe inside the same normalization and
        # log-task finalization boundary as signaling and wait failures.
        parent_already_exited = proc.returncode is not None
        group_exists = _process_group_exists(handle.process_group_id)

        if parent_already_exited and not group_exists:
            if handle.stopped:
                logger.debug(
                    "%s (pid %s) already stopped (code %d); nothing to do",
                    handle.model_name,
                    pid,
                    proc.returncode,
                )
            else:
                logger.info(
                    "%s (pid %s) already exited with code %d",
                    handle.model_name,
                    pid,
                    proc.returncode,
                )
            handle.stopped = True
            return

        if parent_already_exited:
            logger.info(
                "%s (pid %s) already exited with code %d; "
                "stopping remaining process group",
                handle.model_name,
                pid,
                proc.returncode,
            )
        else:
            logger.info(
                "Stopping %s (pid %s) with SIGTERM",
                handle.model_name,
                pid,
            )

        try:
            group_signaled = _signal_process_group(handle, signal.SIGTERM)
        except Exception as exc:
            group_signaled = False
            signal_error = exc
        if not group_signaled and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception as exc:
                signal_error = exc

        try:
            terminated = await _wait_for_stopped(handle, GRACE_PERIOD)
        except Exception as exc:
            signal_error = exc

        if not terminated:
            logger.warning(
                "%s (pid %s) did not exit in %ds, sending SIGKILL",
                handle.model_name,
                pid,
                GRACE_PERIOD,
            )
            try:
                group_signaled = _signal_process_group(
                    handle, signal.SIGKILL
                )
            except Exception as exc:
                group_signaled = False
                signal_error = exc
            if not group_signaled and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    signal_error = exc
            try:
                terminated = await _wait_for_stopped(handle, 5)
            except Exception as exc:
                signal_error = exc
            if not terminated:
                logger.error(
                    "%s (pid %s) could not be killed",
                    handle.model_name,
                    pid,
                )

        if not terminated:
            error = ProcessStopError(
                f"Could not confirm {handle.model_name} (pid {pid}) stopped"
            )
            if signal_error is not None:
                raise error from signal_error
            raise error

        logger.info("%s stopped", handle.model_name)
        handle.stopped = True
    except ProcessStopError:
        raise
    except Exception as exc:
        raise ProcessStopError(
            f"Could not confirm {handle.model_name} (pid {pid}) stopped"
        ) from exc
    finally:
        await _cancel_log_tasks(handle)


def _signal_process_group(handle: ProcessHandle, sig: int) -> bool:
    """Send a signal to a process group. Return False to use fallback."""
    pgid = handle.process_group_id
    if os.name != "posix" or pgid is None:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return handle.process.returncode is not None
    except OSError as exc:
        logger.warning(
            "Could not signal process group %s for %s: %s",
            pgid,
            handle.model_name,
            exc,
        )
        return False


def _process_group_exists(pgid: int | None) -> bool:
    """Return True if a process group still has live members."""
    if os.name != "posix" or pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _wait_for_stopped(handle: ProcessHandle, timeout: float) -> bool:
    """Wait until the tracked process has exited and its group is gone."""
    loop = asyncio.get_running_loop()
    end_time = loop.time() + timeout
    while loop.time() < end_time:
        parent_stopped = handle.process.returncode is not None
        group_stopped = not _process_group_exists(handle.process_group_id)
        if parent_stopped and group_stopped:
            return True
        await asyncio.sleep(0.1)
    return (
        handle.process.returncode is not None
        and not _process_group_exists(handle.process_group_id)
    )


async def _cancel_log_tasks(handle: ProcessHandle) -> None:
    """Cancel stdout/stderr log-draining tasks."""
    for task in (handle._stdout_task, handle._stderr_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def is_running(handle: ProcessHandle) -> bool:
    """Check if the process is still alive."""
    return handle.process.returncode is None
