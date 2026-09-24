from __future__ import annotations

import asyncio

from src.swapper import Swapper
from tests.control_helpers import handle
from tests.quick_switch_helpers import make_config


class TestIdleSettingTransitions:
    async def test_positive_update_restarts_sleeping_timer_and_zero_cancels_it(
        self, monkeypatch
    ):
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        started = asyncio.Queue()

        async def fake_idle(generation):
            await started.put((generation, swapper.idle_timeout, asyncio.current_task()))
            await asyncio.Event().wait()

        monkeypatch.setattr(swapper, "_idle_unload_after_timeout", fake_idle)
        swapper._schedule_idle_timer()
        first_generation, first_timeout, first_task = await asyncio.wait_for(started.get(), 1)
        assert first_timeout == 10

        assert swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=25
        ) == "success"
        second_generation, second_timeout, second_task = await asyncio.wait_for(started.get(), 1)
        assert second_timeout == 25
        assert second_generation > first_generation
        assert second_task is swapper._idle_task
        await asyncio.sleep(0)
        assert first_task.cancelled()

        # Repeating the representation is idempotent and keeps the countdown.
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=25
        )
        assert swapper._idle_task is second_task
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=0
        )
        await asyncio.sleep(0)
        assert second_task.cancelled()
        assert swapper._idle_task is None
        swapper._current = None
        await swapper.shutdown()

    async def test_active_request_defers_new_timeout_until_final_activity_ends(
        self, monkeypatch
    ):
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        started = asyncio.Event()
        observed = []

        async def fake_idle(generation):
            observed.append((generation, swapper.idle_timeout))
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(swapper, "_idle_unload_after_timeout", fake_idle)
        swapper._begin_activity()
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=30
        )
        assert swapper._idle_task is None
        swapper._end_activity()
        await asyncio.wait_for(started.wait(), 1)
        assert observed == [(swapper._idle_generation, 30)]
        swapper._cancel_idle_timer(force=True)
        swapper._current = None
        await swapper.shutdown()

    async def test_setting_change_does_not_cancel_idle_cleanup_after_commit(
        self, monkeypatch
    ):
        import src.swapper as swapper_module
        import src.swapper_lifecycle as lifecycle_module

        sleep_entered = asyncio.Event()
        release_sleep = asyncio.Event()
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            assert delay == 10
            sleep_entered.set()
            await release_sleep.wait()

        async def stop(tracked):
            stop_entered.set()
            await release_stop.wait()
            tracked.process.returncode = 0

        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(swapper_module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        swapper._schedule_idle_timer()
        task = swapper._idle_task
        assert task is not None
        await asyncio.wait_for(sleep_entered.wait(), 1)
        release_sleep.set()
        await asyncio.wait_for(stop_entered.wait(), 1)
        assert swapper._stop_only_operation == "idle"

        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=99
        )
        assert swapper._idle_task is task
        assert not task.cancelled()
        release_stop.set()
        await asyncio.wait_for(task, 1)
        assert swapper.control_state()["lifecycle"] == "idle"
        assert swapper.idle_timeout == 99
        # Restore before shutdown because the monkeypatch altered the shared
        # asyncio module object rather than a private facade.
        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", real_sleep)
        await swapper.shutdown()
