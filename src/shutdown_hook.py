"""Private, direct-exec shutdown hooks with retained process ownership."""

from __future__ import annotations

import asyncio
import os
import shlex

from src.process_manager import ProcessHandle, ProcessStopError, stop_process


async def run_shutdown_hook(handle: ProcessHandle) -> None:
    """Finish a hook attempt, retaining an unresolved group for the next retry.

    Called only from the lifecycle's cancellation-shielded, per-handle task.
    Neither command arguments nor script output enter logs or exceptions.
    """
    if handle.hook_succeeded or handle.shutdown_cmd is None:
        return
    try:
        if handle.hook_process is None:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(handle.shutdown_cmd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            handle.hook_process = ProcessHandle(
                model_name="shutdown hook", port=0, process=process,
                process_group_id=process.pid if os.name == "posix" else None,
            )
            handle.hook_exit_ok = False
            try:
                code = await asyncio.wait_for(
                    process.wait(), timeout=handle.shutdown_timeout
                )
                handle.hook_exit_ok = code == 0
            except TimeoutError:
                pass

        # Even an exited hook may have live children. Do not launch another
        # attempt until this entire group has been confirmed stopped.
        await stop_process(handle.hook_process)
        handle.hook_process = None
        if not handle.hook_exit_ok:
            raise ProcessStopError("Shutdown hook failed")
        handle.hook_succeeded = True
    except Exception:
        raise ProcessStopError("Shutdown hook failed") from None
