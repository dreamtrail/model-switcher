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
    _stdout_task: asyncio.Task | None = None
    _stderr_task: asyncio.Task | None = None


async def _drain_stream(
    reader: asyncio.StreamReader,
    prefix: str,
) -> None:
    """Read lines from a stream and log them with a prefix."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n\r")
            if text:
                _log_upstream_line(prefix, text)
    except asyncio.CancelledError:
        pass


def _log_upstream_line(prefix: str, text: str) -> None:
    """Log an upstream output line at the level indicated by the line."""
    level = _upstream_log_level(text)
    if level == logging.INFO:
        logger.log(
            level,
            "%s: %s",
            prefix,
            text,
            extra={"plain_upstream": True},
        )
        return
    logger.log(level, "%s: %s", prefix, text)


def _upstream_log_level(text: str) -> int:
    """Infer an upstream log level from common severity markers."""
    match = _UPSTREAM_LEVEL_RE.search(text)
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
        model_name: Human-readable model identifier (for log prefix).
        cmd: Shell command to execute.
        port: Port the process should listen on.

    Returns:
        ProcessHandle wrapping the running process.

    Raises:
        subprocess.SubprocessError: if the process fails to start.
    """
    args = shlex.split(cmd)
    prefix = model_name

    logger.info("Starting %s on port %d: %s", prefix, port, cmd)

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
        _drain_stream(process.stdout, prefix),
        name=f"stdout-{prefix}",
    )
    stderr_task = asyncio.create_task(
        _drain_stream(process.stderr, prefix),
        name=f"stderr-{prefix}",
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

    parent_already_exited = proc.returncode is not None
    group_exists = _process_group_exists(handle.process_group_id)

    if parent_already_exited and not group_exists:
        logger.info(
            "%s (pid %s) already exited with code %d",
            handle.model_name,
            pid,
            proc.returncode,
        )
        await _cancel_log_tasks(handle)
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
        logger.info("Stopping %s (pid %s) with SIGTERM", handle.model_name, pid)

    if not _signal_process_group(handle, signal.SIGTERM):
        if proc.returncode is None:
            proc.terminate()

    terminated = await _wait_for_stopped(handle, GRACE_PERIOD)

    if not terminated:
        logger.warning(
            "%s (pid %s) did not exit in %ds, sending SIGKILL",
            handle.model_name,
            pid,
            GRACE_PERIOD,
        )
        if not _signal_process_group(handle, signal.SIGKILL):
            if proc.returncode is None:
                proc.kill()
        terminated = await _wait_for_stopped(handle, 5)
        if not terminated:
            logger.error(
                "%s (pid %s) could not be killed", handle.model_name, pid
            )

    await _cancel_log_tasks(handle)

    if terminated:
        logger.info("%s stopped", handle.model_name)


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
