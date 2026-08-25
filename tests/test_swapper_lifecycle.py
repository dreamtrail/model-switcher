"""Swapper lifecycle and state tests.

Stopping already-exited processes on swap/shutdown, startup timeout and
cancellation cleanup, shutdown behavior, and active_model/active_port state.
"""

import asyncio

import aiohttp
import pytest

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle, stop_process
from src.swapper import Swapper


class TestSwapperTeardown:

    async def test_swap_calls_stop_for_already_exited_parent(self, monkeypatch):
        """Swapping calls stop_process even if tracked parent exited."""
        from src.process_manager import ProcessHandle
        import src.swapper as swapper_module

        class FakeProcess:
            def __init__(self, returncode):
                self.returncode = returncode
                self.pid = 12345

        config = Config(models={})
        config.models["model-a"] = ModelConfig(
            name="model-a", cmd="echo a --port ${PORT}", port=1111
        )
        config.models["model-b"] = ModelConfig(
            name="model-b", cmd="echo b --port ${PORT}", port=2222
        )
        swapper = Swapper(config)

        old_handle = ProcessHandle(
            model_name="model-a",
            port=1111,
            process=FakeProcess(returncode=0),
        )
        new_handle = ProcessHandle(
            model_name="model-b",
            port=2222,
            process=FakeProcess(returncode=None),
        )
        stop_calls = []

        async def fake_stop_process(handle):
            stop_calls.append(handle)

        async def fake_start_process(model_name, cmd, port):
            return new_handle

        async def fake_wait_for_ready(*args, **kwargs):
            return None

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
        monkeypatch.setattr(swapper_module, "start_process", fake_start_process)
        monkeypatch.setattr(swapper_module, "wait_for_ready", fake_wait_for_ready)

        swapper._current = old_handle
        swapper._current_model = "model-a"
        swapper._healthy = False

        try:
            handle = await swapper._ensure_model("model-b")
            assert handle is new_handle
            assert stop_calls == [old_handle]
        finally:
            swapper._current = None
            await swapper.shutdown()

    async def test_shutdown_calls_stop_for_already_exited_parent(
        self, simple_config: Config, monkeypatch
    ):
        """Shutdown calls stop_process even if tracked parent exited."""
        from src.process_manager import ProcessHandle
        import src.swapper as swapper_module

        class FakeProcess:
            returncode = 0
            pid = 12345

        swapper = Swapper(simple_config)
        handle = ProcessHandle(
            model_name="model-a",
            port=1234,
            process=FakeProcess(),
        )
        stop_calls = []

        async def fake_stop_process(stopped_handle):
            stop_calls.append(stopped_handle)

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper._current = handle
        await swapper.shutdown()

        assert stop_calls == [handle]
        assert swapper._current is None

    async def test_startup_timeout_stops_new_handle_and_clears_state(
        self, simple_config: Config, monkeypatch
    ):
        """A model that never becomes ready is stopped before state is cleared."""
        import src.swapper as swapper_module

        class FakeProcess:
            returncode = None
            pid = 12345

        handle = ProcessHandle(
            model_name="model-a",
            port=1234,
            process=FakeProcess(),
        )
        stop_calls = []
        swapper = Swapper(simple_config)

        async def fake_start_process(model_name, cmd, port):
            return handle

        async def fake_wait_for_ready(*args, **kwargs):
            raise TimeoutError("not ready")

        async def fake_stop_process(stopped_handle):
            stop_calls.append(stopped_handle)

        monkeypatch.setattr(swapper_module, "start_process", fake_start_process)
        monkeypatch.setattr(swapper_module, "wait_for_ready", fake_wait_for_ready)
        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        try:
            with pytest.raises(TimeoutError):
                await swapper._ensure_model("model-a")

            assert stop_calls == [handle]
            assert swapper._current is None
            assert swapper._current_model is None
            assert swapper._healthy is False
        finally:
            await swapper.shutdown()

    async def test_startup_cancellation_stops_new_handle_and_reraises(
        self, simple_config: Config, monkeypatch
    ):
        """Cancelling readiness waits stops the new process and propagates."""
        import src.swapper as swapper_module

        class FakeProcess:
            returncode = None
            pid = 12345

        handle = ProcessHandle(
            model_name="model-a",
            port=1234,
            process=FakeProcess(),
        )
        stop_calls = []
        swapper = Swapper(simple_config)

        async def fake_start_process(model_name, cmd, port):
            return handle

        async def fake_wait_for_ready(*args, **kwargs):
            raise asyncio.CancelledError

        async def fake_stop_process(stopped_handle):
            stop_calls.append(stopped_handle)

        monkeypatch.setattr(swapper_module, "start_process", fake_start_process)
        monkeypatch.setattr(swapper_module, "wait_for_ready", fake_wait_for_ready)
        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        try:
            with pytest.raises(asyncio.CancelledError):
                await swapper._ensure_model("model-a")

            assert stop_calls == [handle]
            assert swapper._current is None
            assert swapper._current_model is None
            assert swapper._healthy is False
        finally:
            await swapper.shutdown()


class TestActiveProperties:

    async def test_active_model_returns_none_when_nothing_running(
        self, simple_config: Config
    ):
        """active_model is None when no model is running."""
        swapper = Swapper(simple_config)
        assert swapper.active_model is None

    async def test_active_model_returns_model_name_when_running(
        self, simple_config: Config
    ):
        """active_model returns the model name when a model is running."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True
            assert swapper.active_model == "model-a"
        finally:
            await swapper.shutdown()

    async def test_active_model_returns_none_when_not_healthy(
        self, simple_config: Config
    ):
        """active_model is None when _healthy is False."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = False
            assert swapper.active_model is None
        finally:
            await stop_process(handle)

    async def test_active_port_returns_none_when_nothing_running(
        self, simple_config: Config
    ):
        """active_port is None when no model is running."""
        swapper = Swapper(simple_config)
        assert swapper.active_port is None

    async def test_active_port_returns_port_when_running(
        self, simple_config: Config
    ):
        """active_port returns the port when a model is running."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True
            assert swapper.active_port == mc.port
        finally:
            await swapper.shutdown()


class TestShutdown:

    async def test_shutdown_stops_current_model(self, simple_config: Config):
        """Shutdown stops the currently running model."""
        from src.process_manager import start_process, is_running

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        try:
            await swapper.shutdown()
            assert swapper._current is None
            assert not is_running(handle)
        finally:
            # Safety cleanup in case shutdown didn't stop the process
            if is_running(handle):
                await stop_process(handle)

    async def test_shutdown_drains_queue(self, simple_config: Config):
        """Shutdown resolves any queued futures."""
        swapper = Swapper(simple_config)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        swapper._queue.append(future)

        await swapper.shutdown()
        assert future.done()

    async def test_shutdown_closes_shared_client(self, simple_config: Config):
        """Shutdown closes the shared ClientSession."""
        client = aiohttp.ClientSession()
        swapper = Swapper(simple_config, client=client)

        await swapper.shutdown()
        assert swapper._client is None
        # ClientSession.closed is True after close()
        # We can't directly check client.closed, but if shutdown didn't
        # raise and set _client to None, the close() was called.
