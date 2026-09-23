"""Hook lifecycle coverage, including real local launch/stop ordering."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle, ProcessStopError
from src.swapper import Swapper
from src.swapper_lifecycle import _CleanupFailed, _ModelStartFailed
from tests.test_shutdown_hook import command


def make_swapper(cmd="echo", hook=None, **kwargs):
    return Swapper(Config(models={
        "a": ModelConfig("a", cmd, 1111, hook),
        "b": ModelConfig("b", cmd, 2222),
    }), **kwargs)


async def wait_file(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.01)


async def test_real_launch_hook_local_stop_then_replacement(tmp_path, monkeypatch):
    events = tmp_path / "events"
    ready_a, ready_b = tmp_path / "a", tmp_path / "b"
    def launcher(name, ready):
        return command(
            "import signal,time; from pathlib import Path; "
            f"p=Path({str(events)!r}); "
            "write=lambda s: p.open('a').write(s+'\\n'); "
            f"signal.signal(signal.SIGTERM,lambda *_: (write('stop {name}'),exit(0))); "
            f"write('launch {name}'); Path({str(ready)!r}).touch(); time.sleep(60)"
        )
    hook = command(
        "from pathlib import Path; "
        f"Path({str(events)!r}).open('a').write('hook a\\n')"
    )
    swapper = make_swapper(hook=hook)
    # Supply already-resolved commands; absolute Python binaries are not
    # startup scripts for the existing port-rewriter.
    swapper.config.models["a"].cmd = launcher("a", ready_a)
    swapper.config.models["b"].cmd = launcher("b", ready_b)
    async def ready(port, **kwargs):
        await wait_file(ready_a if port == 1111 else ready_b)
    monkeypatch.setattr(swapper, "_wait_for_ready", ready)
    try:
        await swapper._ensure_model("a")
        # Mutating configuration must not alter the running handle's snapshot.
        swapper.config.models["a"].shutdown_cmd = "/missing/changed"
        await swapper._ensure_model("b")
        assert events.read_text().splitlines() == ["launch a", "hook a", "stop a", "launch b"]
    finally:
        await swapper.shutdown()


@pytest.mark.parametrize("path", ["manual", "idle", "shutdown", "timeout", "unexpected", "cancel", "exited"])
async def test_stop_paths_run_hook(tmp_path, monkeypatch, path):
    marker = tmp_path / "hook"
    hook = command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
    swapper = make_swapper(hook=hook, idle_timeout=0.001)
    h = ProcessHandle("a", 1111, Mock(returncode=None))
    monkeypatch.setattr(swapper, "_start_process", AsyncMock(return_value=h))
    stop = AsyncMock()
    monkeypatch.setattr(swapper, "_stop_process", stop)
    entered = asyncio.Event()
    async def ready(*args, **kwargs):
        entered.set()
        if path == "timeout":
            raise TimeoutError
        if path == "unexpected":
            raise RuntimeError("private readiness details")
        if path == "cancel":
            await asyncio.Event().wait()
    monkeypatch.setattr(swapper, "_wait_for_ready", ready)
    try:
        if path in {"timeout", "unexpected"}:
            with pytest.raises(TimeoutError if path == "timeout" else _ModelStartFailed):
                await swapper._ensure_model("a")
        elif path == "cancel":
            owner = asyncio.create_task(swapper._ensure_model("a"))
            await entered.wait()
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
        else:
            await swapper._ensure_model("a")
            if path == "manual":
                assert await swapper.control_unload_model() == "success"
            elif path == "idle":
                await swapper._idle_unload_after_timeout(swapper._idle_generation)
            elif path == "exited":
                h.process.returncode = 0
                assert await swapper.control_unload_model() == "success"
            else:
                await swapper.shutdown()
        assert marker.exists()
        stop.assert_awaited_once_with(h)
        assert swapper._current is None
    finally:
        await swapper.shutdown()


@pytest.mark.parametrize("failed_stage", ["hook", "local"])
async def test_retry_only_unfinished_stage_blocks_replacement(monkeypatch, failed_stage):
    import src.swapper_lifecycle as lifecycle
    swapper = make_swapper(hook="private")
    h = ProcessHandle("a", 1111, Mock(returncode=None), shutdown_cmd="private")
    swapper._commit_started_handle(h)
    counts = {"hook": 0, "local": 0}
    async def hook(handle):
        if handle.hook_succeeded:
            return
        counts["hook"] += 1
        if failed_stage == "hook" and counts["hook"] == 1:
            raise ProcessStopError("private")
        handle.hook_succeeded = True
    async def local(handle):
        counts["local"] += 1
        if failed_stage == "local" and counts["local"] == 1:
            raise ProcessStopError("private")
    monkeypatch.setattr(lifecycle, "run_shutdown_hook", hook)
    monkeypatch.setattr(swapper, "_stop_process", local)
    spawn = AsyncMock()
    monkeypatch.setattr(swapper, "_start_process", spawn)
    record_stop = Mock(wraps=swapper.stats.record_model_stop)
    monkeypatch.setattr(swapper.stats, "record_model_stop", record_stop)
    try:
        with pytest.raises(_CleanupFailed):
            await swapper._ensure_model("b")
        spawn.assert_not_called()
        assert swapper._current is h and swapper._cleanup_failed
        assert await swapper.control_unload_model() == "success"
        await swapper._stop_tracked(h, record_stop=True)
        assert counts[failed_stage] == 2
        assert counts["local" if failed_stage == "hook" else "hook"] == 1
        record_stop.assert_called_once_with("a")
    finally:
        await swapper.shutdown()


async def test_concurrent_cleanup_and_repeated_cancellation(monkeypatch):
    import src.swapper_lifecycle as lifecycle
    swapper = make_swapper()
    h = ProcessHandle("a", 1111, Mock(returncode=None))
    swapper._commit_started_handle(h)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hook(handle):
        entered.set()
        await release.wait()
    hook_mock = AsyncMock(side_effect=hook)
    monkeypatch.setattr(lifecycle, "run_shutdown_hook", hook_mock)
    stop = AsyncMock()
    monkeypatch.setattr(swapper, "_stop_process", stop)
    first = asyncio.create_task(swapper._stop_tracked(h, record_stop=True))
    await entered.wait()
    second = asyncio.create_task(swapper._stop_tracked(h, record_stop=True))
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not h.cleanup_task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    hook_mock.assert_awaited_once()
    stop.assert_awaited_once()
    assert swapper._current is None
    await swapper.shutdown()


async def test_cancelled_spawn_snapshots_hook_and_failed_exec_does_not_run_it(tmp_path, monkeypatch):
    marker = tmp_path / "hook"
    hook = command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
    swapper = make_swapper(hook=hook)
    h = ProcessHandle("a", 1111, Mock(returncode=None))
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*args):
        entered.set()
        await release.wait()
        return h
    monkeypatch.setattr(swapper, "_start_process", spawn)
    monkeypatch.setattr(swapper, "_stop_process", AsyncMock())
    task = asyncio.create_task(swapper._ensure_model("a"))
    await entered.wait()
    swapper.config.models["a"].shutdown_cmd = "/missing/changed"
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert marker.exists() and h.shutdown_cmd == hook
    marker.unlink()
    monkeypatch.setattr(swapper, "_start_process", AsyncMock(side_effect=OSError("exec failed")))
    with pytest.raises(_ModelStartFailed):
        await swapper._ensure_model("a")
    assert not marker.exists()
    await swapper.shutdown()


async def test_supersession_runs_hook_before_new_model(tmp_path, monkeypatch):
    marker = tmp_path / "hook"
    swapper = make_swapper(
        hook=command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        quick_switch=True,
    )
    entered = asyncio.Event()
    async def spawn(name, cmd, port):
        if name == "b":
            assert marker.exists()
        return ProcessHandle(name, port, Mock(returncode=None))
    async def ready(port, **kwargs):
        if port == 1111:
            entered.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(swapper, "_start_process", spawn)
    monkeypatch.setattr(swapper, "_stop_process", AsyncMock())
    monkeypatch.setattr(swapper, "_wait_for_ready", ready)
    try:
        a = asyncio.create_task(swapper.control_load_model("a"))
        await entered.wait()
        b = asyncio.create_task(swapper.control_load_model("b"))
        assert await a == "swap_superseded"
        assert await b == "success"
        assert marker.exists()
    finally:
        await swapper.shutdown()


async def test_control_failure_redacts_hook_and_blocks_replacement(monkeypatch, caplog):
    from aiohttp.test_utils import TestClient
    from tests.quick_switch_helpers import make_server
    from src.control import CONTROL_HEADER

    swapper = make_swapper(hook="/missing/PRIVATE_HOOK --token PRIVATE_TOKEN")
    spawn = AsyncMock(side_effect=lambda name, cmd, port: ProcessHandle(
        name, port, Mock(returncode=None)
    ))
    monkeypatch.setattr(swapper, "_start_process", spawn)
    monkeypatch.setattr(swapper, "_stop_process", AsyncMock())
    monkeypatch.setattr(swapper, "_wait_for_ready", AsyncMock())
    server = await make_server(swapper)
    try:
        async with TestClient(server) as client:
            await swapper.control_load_model("a")
            response = await client.put(
                "/api/v1/control/model", json={"model": "b"},
                headers={CONTROL_HEADER: "1"},
            )
            assert response.status == 503
            body = await response.text()
            assert "process_cleanup_failed" in body
            state = await client.get("/api/v1/control/state")
            assert "PRIVATE" not in body + await state.text() + caplog.text
            assert spawn.await_count == 1
            assert swapper._current.local_cleanup_done
    finally:
        # Test-only recovery of the intentionally unexecutable hook.
        if swapper._current is not None:
            swapper._current.shutdown_cmd = None
        await swapper.shutdown()
